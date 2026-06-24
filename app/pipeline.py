from __future__ import annotations

import argparse
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import UsedPost
from app.services.captions import generate as generate_captions
from app.services.reddit_scraper import RedditPost, fetch_eligible_post
from app.services.script_generator import Script, generate as generate_script
from app.services.video_assembler import AssembleResult, assemble
from app.services.voiceover import VoiceoverResult, generate as generate_voiceover
from app.services.youtube_uploader import UploadResult, upload
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Serializes pipeline runs within a process so a manual trigger and the
# scheduled job (or two manual triggers) can't run concurrently and burn paid
# API calls twice. Cross-process safety relies on the DB reddit_id claim.
_run_lock = threading.Lock()


@dataclass(frozen=True)
class PipelineResult:
    skipped: bool = False
    success: bool = False
    reason: str = ""
    reddit_id: Optional[str] = None
    post: Optional[RedditPost] = None
    script: Optional[Script] = None
    voiceover: Optional[VoiceoverResult] = None
    video: Optional[AssembleResult] = None
    upload: Optional[UploadResult] = None
    used_post: Optional[UsedPost] = None
    ready_path: Optional[Path] = None


def _copy_to_ready(video_path: Path, reddit_id: str, settings: Settings) -> Path:
    """Place a copy of the finished video in the flat ready/ dir for manual posting."""
    settings.ready_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.ready_dir / f"{reddit_id}.mp4"
    shutil.copy2(video_path, dest)
    return dest


def run_pipeline(
    db: Session,
    *,
    settings: Optional[Settings] = None,
    skip_upload: bool = False,
) -> PipelineResult:
    """Run the full AITA video pipeline for one eligible Reddit post."""
    settings = settings or get_settings()
    post = fetch_eligible_post(db, settings=settings)

    if post is None:
        logger.info("Pipeline skipped: no eligible posts found")
        return PipelineResult(skipped=True, reason="no_eligible_posts")

    logger.info(
        "Pipeline starting for post %s (score=%d): %s",
        post.id,
        post.score,
        post.title,
    )

    try:
        # Generation stages are cheap and reproducible: nothing is recorded
        # here, so a failure leaves no DB row and the post is retried.
        script = generate_script(post, settings=settings)
        voiceover = generate_voiceover(script.full_text, post.id, settings=settings)
        generate_captions(script, voiceover, post.id, settings=settings)
        video = assemble(post.id, voiceover, settings=settings)
        ready_path = _copy_to_ready(video.video_path, post.id, settings)

        used_post: Optional[UsedPost] = None
        upload_result: Optional[UploadResult] = None

        if not skip_upload:
            # Claim the post (commit a row) BEFORE the irreversible upload. If a
            # later step or commit fails, the claimed reddit_id keeps the post
            # from being re-selected, so we never upload a duplicate on retry.
            used_post = UsedPost(
                reddit_id=post.id,
                title=post.title[:500],
                upvotes=post.score,
                script_word_count=script.word_count,
                video_path=str(video.video_path),
                status="uploading",
            )
            db.add(used_post)
            db.commit()

            try:
                upload_result = upload(video.video_path, post, script, settings=settings)
            except Exception:
                used_post.status = "upload_failed"
                db.commit()
                raise

            used_post.youtube_video_id = upload_result.youtube_video_id
            used_post.status = "completed"
            used_post.processed_at = datetime.now(timezone.utc)
            db.commit()

        logger.info(
            "Pipeline completed for post %s%s",
            post.id,
            f" -> {upload_result.youtube_url}" if upload_result else " (upload skipped)",
        )

        return PipelineResult(
            success=True,
            reddit_id=post.id,
            post=post,
            script=script,
            voiceover=voiceover,
            video=video,
            upload=upload_result,
            used_post=used_post,
            ready_path=ready_path,
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Pipeline failed for post %s", post.id)
        return PipelineResult(
            success=False,
            reason=str(exc),
            reddit_id=post.id,
            post=post,
        )


def run_pipeline_locked(
    db: Session,
    *,
    settings: Optional[Settings] = None,
    skip_upload: bool = False,
) -> PipelineResult:
    """run_pipeline guarded by a process-wide lock.

    If a run is already in progress, returns immediately with
    skipped/reason="already_running" instead of starting a concurrent run.
    """
    if not _run_lock.acquire(blocking=False):
        logger.info("Pipeline already running; skipping this trigger")
        return PipelineResult(skipped=True, reason="already_running")
    try:
        return run_pipeline(db, settings=settings, skip_upload=skip_upload)
    finally:
        _run_lock.release()


def _print_result(result: PipelineResult) -> None:
    if result.skipped:
        print(f"Skipped: {result.reason}")
        return

    if not result.success:
        print(f"Failed ({result.reddit_id}): {result.reason}")
        return

    assert result.post is not None
    assert result.script is not None
    assert result.video is not None

    print(f"Post:      {result.post.title}")
    print(f"Reddit ID: {result.reddit_id}")
    print(f"Script:    {result.script.word_count} words")
    print(f"Video:     {result.video.video_path}")
    print(f"Duration:  {result.video.duration_seconds:.1f}s")
    if result.ready_path:
        print(f"Ready:     {result.ready_path}  (manual TikTok upload)")
    if result.upload:
        print(f"YouTube:   {result.upload.youtube_url}")
    else:
        print("YouTube:   skipped")
    if result.used_post:
        print(f"DB record: used_posts.id={result.used_post.id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AITA video pipeline once.")
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Assemble video but do not upload to YouTube or record in the database",
    )
    args = parser.parse_args()

    settings = get_settings()
    if args.skip_upload:
        missing = [
            name
            for name in (
                "openai_api_key",
                "elevenlabs_api_key",
                "elevenlabs_voice_id",
            )
            if not getattr(settings, name)
        ]
        if missing:
            raise SystemExit(
                "Missing configuration for pipeline: " + ", ".join(sorted(missing))
            )
    else:
        settings.validate_required()
        if not settings.youtube_client_secrets_file.is_file():
            raise SystemExit(
                f"Missing YouTube client secrets: {settings.youtube_client_secrets_file}"
            )
        if not settings.youtube_token_file.is_file():
            raise SystemExit(
                "Missing YouTube token. Run: python -m app.services.youtube_uploader --auth"
            )

    from app.database import get_db_session, init_db

    init_db()

    with get_db_session() as db:
        result = run_pipeline(db, settings=settings, skip_upload=args.skip_upload)

    _print_result(result)
    if result.skipped:
        raise SystemExit(0)
    if not result.success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
