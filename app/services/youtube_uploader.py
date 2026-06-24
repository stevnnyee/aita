from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import Settings, get_settings
from app.services.reddit_scraper import RedditPost
from app.services.script_generator import Script
from app.utils.logging import get_logger

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_CATEGORY_ENTERTAINMENT = "24"
DEFAULT_TAGS = ("AITA", "Reddit", "AmItheAsshole", "Am I The Asshole", "Shorts")
MAX_TITLE_LENGTH = 100


@dataclass(frozen=True)
class UploadResult:
    youtube_video_id: str
    youtube_url: str
    title: str


def _validate_youtube_files(settings: Settings) -> None:
    if not settings.youtube_client_secrets_file.is_file():
        raise FileNotFoundError(
            "YouTube OAuth client secrets not found at "
            f"{settings.youtube_client_secrets_file}. "
            "Download OAuth credentials from Google Cloud Console."
        )


def _load_credentials(settings: Settings):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = settings.youtube_token_file
    creds = None

    if token_file.is_file():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")
        return creds

    raise RuntimeError(
        "YouTube credentials are missing or invalid. "
        "Run: python -m app.services.youtube_uploader --auth"
    )


def _save_credentials(settings: Settings, creds) -> None:
    settings.youtube_token_file.parent.mkdir(parents=True, exist_ok=True)
    settings.youtube_token_file.write_text(creds.to_json(), encoding="utf-8")


def authenticate(settings: Optional[Settings] = None) -> None:
    """Run the one-time OAuth browser flow and save the refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    settings = settings or get_settings()
    _validate_youtube_files(settings)

    flow = InstalledAppFlow.from_client_secrets_file(
        str(settings.youtube_client_secrets_file),
        SCOPES,
    )
    creds = flow.run_local_server(port=0)
    _save_credentials(settings, creds)
    logger.info("YouTube OAuth complete. Token saved to %s", settings.youtube_token_file)


def get_youtube_service(settings: Optional[Settings] = None):
    from googleapiclient.discovery import build

    settings = settings or get_settings()
    _validate_youtube_files(settings)
    creds = _load_credentials(settings)
    return build("youtube", "v3", credentials=creds)


def _truncate_title(title: str, max_length: int = MAX_TITLE_LENGTH) -> str:
    cleaned = " ".join(title.split())
    if len(cleaned) <= max_length:
        return cleaned
    return cleaned[: max_length - 3].rstrip() + "..."


def _build_title(post: RedditPost) -> str:
    return _truncate_title(post.title)


def _build_description(post: RedditPost, script: Script) -> str:
    verdict_line = script.verdict.strip() or "Watch for the full AITA verdict."
    return (
        f"{verdict_line}\n\n"
        f"Original post: {post.url}\n\n"
        "#AITA #Reddit #AmItheAsshole #Shorts"
    )


def upload(
    video_path: Path,
    post: RedditPost,
    script: Script,
    *,
    settings: Optional[Settings] = None,
) -> UploadResult:
    """Upload a video to YouTube and return the new video ID."""
    from googleapiclient.http import MediaFileUpload

    settings = settings or get_settings()
    video_path = Path(video_path)

    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    youtube = get_youtube_service(settings)
    title = _build_title(post)
    description = _build_description(post, script)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": list(DEFAULT_TAGS),
            "categoryId": YOUTUBE_CATEGORY_ENTERTAINMENT,
        },
        "status": {
            "privacyStatus": settings.youtube_privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        chunksize=1024 * 1024 * 8,
        resumable=True,
    )
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            progress = int(status.progress() * 100)
            logger.info("YouTube upload progress: %d%%", progress)

    video_id = response["id"]
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"

    logger.info("Uploaded video for post %s -> %s", post.id, youtube_url)

    return UploadResult(
        youtube_video_id=video_id,
        youtube_url=youtube_url,
        title=title,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube OAuth and upload utilities.")
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Run OAuth flow and save credentials/youtube_token.json",
    )
    parser.add_argument(
        "--upload",
        type=Path,
        default=None,
        help="Upload this MP4 to YouTube",
    )
    parser.add_argument(
        "--title",
        default="AITA - Sample Upload",
        help="Video title when using --upload without a Reddit post",
    )
    parser.add_argument(
        "--url",
        default="https://www.reddit.com/r/AmItheAsshole/",
        help="Original post URL for the description",
    )
    args = parser.parse_args()

    settings = get_settings()

    if args.auth:
        authenticate(settings)
        print(f"Token saved to {settings.youtube_token_file}")
        return

    if args.upload:
        post = RedditPost(
            id="manual",
            title=args.title,
            body="",
            score=0,
            url=args.url,
            author="manual",
            created_utc=0.0,
        )
        script = Script(
            full_text="",
            hook="",
            story="",
            verdict="NTA — sample upload from the AITA pipeline.",
            cta="",
            word_count=0,
        )
        result = upload(args.upload, post, script, settings=settings)
        print(f"Title:  {result.title}")
        print(f"URL:    {result.youtube_url}")
        return

    raise SystemExit("Use --auth or --upload <path>")


if __name__ == "__main__":
    main()
