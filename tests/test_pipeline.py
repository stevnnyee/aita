from __future__ import annotations

import pytest

from app.config import Settings
from app.models import UsedPost
from app.services.reddit_scraper import RedditPost
from app.services.script_generator import Script
from app.services.voiceover import VoiceoverResult
from app.services.captions import CaptionResult
from app.services.video_assembler import AssembleResult
from app.services.youtube_uploader import UploadResult
from app import pipeline as pl


def make_post():
    return RedditPost(
        id="abc123", title="AITA for testing?", body="body", score=900,
        url="https://reddit.com/r/x/abc123", author="u", created_utc=0.0,
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(output_dir=tmp_path / "output", ready_dir=tmp_path / "ready")


@pytest.fixture
def wired(tmp_path, monkeypatch, settings):
    """Wire all pipeline stages to fakes; return the call log + a real video file."""
    calls = []
    video_file = tmp_path / "final.mp4"
    video_file.write_bytes(b"video-bytes")

    post = make_post()
    script = Script(full_text="t", hook="h", story="s", verdict="NTA", cta="c", word_count=42)
    vo = VoiceoverResult(
        audio_path=tmp_path / "vo.mp3", alignment_path=tmp_path / "a.json",
        duration_seconds=62.0, alignment=None,
    )
    video = AssembleResult(
        video_path=video_file, duration_seconds=67.0,
        background_path=tmp_path / "bg.mp4", music_path=tmp_path / "m.mp3",
    )

    monkeypatch.setattr(pl, "fetch_eligible_post", lambda db, settings: (calls.append("fetch"), post)[1])
    monkeypatch.setattr(pl, "generate_script", lambda p, settings: (calls.append("script"), script)[1])
    monkeypatch.setattr(pl, "generate_voiceover", lambda text, rid, settings: (calls.append("vo"), vo)[1])
    monkeypatch.setattr(pl, "generate_captions", lambda s, v, rid, settings: (calls.append("captions"), CaptionResult(tmp_path / "c.ass", 5))[1])
    monkeypatch.setattr(pl, "assemble", lambda rid, v, settings: (calls.append("assemble"), video)[1])
    monkeypatch.setattr(
        pl, "upload",
        lambda vp, p, s, settings: (calls.append("upload"), UploadResult("YT1", "https://youtu.be/YT1", "title"))[1],
    )
    return calls, post, script, vo, video


# --- skip when no eligible post ---------------------------------------------

def test_run_pipeline_no_eligible_post(db_session, settings, monkeypatch):
    monkeypatch.setattr(pl, "fetch_eligible_post", lambda db, settings: None)
    result = pl.run_pipeline(db_session, settings=settings)
    assert result.skipped and result.reason == "no_eligible_posts"
    assert db_session.query(UsedPost).count() == 0


# --- skip_upload (dev) -------------------------------------------------------

def test_run_pipeline_skip_upload(db_session, settings, wired):
    calls, post, *_ = wired
    result = pl.run_pipeline(db_session, settings=settings, skip_upload=True)

    assert result.success and result.upload is None
    assert "upload" not in calls               # upload never attempted
    assert db_session.query(UsedPost).count() == 0  # no DB record in dev mode
    assert result.ready_path.is_file()         # but the video is queued for posting
    assert result.ready_path.name == "abc123.mp4"


# --- full success ------------------------------------------------------------

def test_run_pipeline_full_success(db_session, settings, wired):
    calls, post, script, *_ = wired
    result = pl.run_pipeline(db_session, settings=settings)

    assert result.success
    assert calls == ["fetch", "script", "vo", "captions", "assemble", "upload"]
    assert result.upload.youtube_video_id == "YT1"

    row = db_session.query(UsedPost).one()
    assert row.reddit_id == "abc123"
    assert row.status == "completed"
    assert row.youtube_video_id == "YT1"
    assert row.script_word_count == 42
    assert row.processed_at is not None
    assert result.ready_path.is_file()


# --- generation failure (retriable: no row) ---------------------------------

def test_run_pipeline_generation_failure_leaves_no_record(db_session, settings, wired, monkeypatch):
    def boom(rid, v, settings):
        raise RuntimeError("ffmpeg blew up")

    monkeypatch.setattr(pl, "assemble", boom)
    result = pl.run_pipeline(db_session, settings=settings)

    assert not result.success
    assert "ffmpeg blew up" in result.reason
    assert db_session.query(UsedPost).count() == 0  # retriable: post not claimed


# --- upload failure (claimed: blocks duplicate) ------------------------------

def test_run_pipeline_upload_failure_marks_row(db_session, settings, wired, monkeypatch):
    def boom(vp, p, s, settings):
        raise RuntimeError("youtube 403")

    monkeypatch.setattr(pl, "upload", boom)
    result = pl.run_pipeline(db_session, settings=settings)

    assert not result.success
    row = db_session.query(UsedPost).one()  # row persists -> post won't re-upload
    assert row.status == "upload_failed"
    assert row.youtube_video_id is None


# --- commit fails AFTER successful upload (no duplicate on retry) ------------

def test_run_pipeline_claim_survives_final_commit_failure(db_session, settings, wired, monkeypatch):
    real_commit = db_session.commit
    state = {"n": 0}

    def flaky_commit():
        state["n"] += 1
        if state["n"] == 2:  # 1=claim (ok), 2=completion (fails)
            raise RuntimeError("db write failed")
        return real_commit()

    monkeypatch.setattr(db_session, "commit", flaky_commit)
    result = pl.run_pipeline(db_session, settings=settings)

    assert not result.success
    # The claim row (committed first) survives, so a retry's fetch_eligible_post
    # would exclude this reddit_id -> the already-uploaded video is not re-posted.
    row = db_session.query(UsedPost).filter_by(reddit_id="abc123").one()
    assert row.status == "uploading"
