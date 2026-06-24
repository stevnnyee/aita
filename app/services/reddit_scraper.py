from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import UsedPost
from app.utils.logging import get_logger

if TYPE_CHECKING:
    import praw
    from praw.models import Submission

logger = get_logger(__name__)

REMOVED_MARKERS = {"[removed]", "[deleted]"}
MIN_BODY_CHARS = 50
MIN_TITLE_ONLY_CHARS = 100
FETCH_LIMIT = 50


@dataclass(frozen=True)
class RedditPost:
    id: str
    title: str
    body: str
    score: int
    url: str
    author: str
    created_utc: float


def _reddit_client(settings: Settings) -> "praw.Reddit":
    import praw

    return praw.Reddit(
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        user_agent=settings.reddit_user_agent,
    )


def _used_reddit_ids(db: Session) -> set[str]:
    return set(db.scalars(select(UsedPost.reddit_id)).all())


def _submission_url(submission: Submission) -> str:
    return f"https://www.reddit.com{submission.permalink}"


def _author_name(submission: Submission) -> str:
    if submission.author is None:
        return "[deleted]"
    return str(submission.author.name)


def _extract_body(submission: Submission) -> Optional[str]:
    title = (submission.title or "").strip()
    selftext = (submission.selftext or "").strip()

    if selftext.lower() in REMOVED_MARKERS:
        return None
    if title.lower() in REMOVED_MARKERS:
        return None

    if selftext:
        return selftext

    # Image/link posts sometimes put the whole story in the title.
    if len(title) >= MIN_TITLE_ONLY_CHARS:
        return title

    return None


def _is_eligible_submission(
    submission: Submission,
    *,
    min_upvotes: int,
    cutoff_utc: float,
    used_ids: set[str],
) -> bool:
    if submission.id in used_ids:
        return False

    if submission.stickied:
        return False

    if submission.score < min_upvotes:
        return False

    if submission.created_utc < cutoff_utc:
        return False

    body = _extract_body(submission)
    if body is None or len(body) < MIN_BODY_CHARS:
        return False

    return True


def _to_reddit_post(submission: Submission) -> RedditPost:
    body = _extract_body(submission)
    if body is None:
        raise ValueError(f"Submission {submission.id} has no usable body")

    return RedditPost(
        id=submission.id,
        title=(submission.title or "").strip(),
        body=body,
        score=submission.score,
        url=_submission_url(submission),
        author=_author_name(submission),
        created_utc=float(submission.created_utc),
    )


def fetch_eligible_posts(
    db: Session,
    *,
    settings: Optional[Settings] = None,
    limit: int = FETCH_LIMIT,
) -> list[RedditPost]:
    """Return all eligible top posts, highest score first."""
    settings = settings or get_settings()
    reddit = _reddit_client(settings)
    subreddit = reddit.subreddit(settings.subreddit_name)

    cutoff_utc = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()
    used_ids = _used_reddit_ids(db)

    eligible: list[RedditPost] = []
    examined = 0

    for submission in subreddit.top(time_filter="day", limit=limit):
        examined += 1
        if not _is_eligible_submission(
            submission,
            min_upvotes=settings.min_upvotes,
            cutoff_utc=cutoff_utc,
            used_ids=used_ids,
        ):
            continue
        eligible.append(_to_reddit_post(submission))

    logger.info(
        "Reddit scan complete: examined=%d eligible=%d subreddit=r/%s min_upvotes=%d",
        examined,
        len(eligible),
        settings.subreddit_name,
        settings.min_upvotes,
    )
    return eligible


def fetch_eligible_post(
    db: Session,
    *,
    settings: Optional[Settings] = None,
) -> Optional[RedditPost]:
    """Return the highest-scoring unused eligible post, or None."""
    posts = fetch_eligible_posts(db, settings=settings)
    if not posts:
        return None
    return posts[0]


def _validate_reddit_config(settings: Settings) -> None:
    missing = [
        name
        for name in ("reddit_client_id", "reddit_client_secret", "reddit_user_agent")
        if not getattr(settings, name)
    ]
    if missing:
        raise ValueError(
            "Missing Reddit configuration: "
            + ", ".join(sorted(missing))
            + ". Set these via environment variables or the .env file."
        )


def _format_post(post: RedditPost) -> str:
    created = datetime.fromtimestamp(post.created_utc, tz=timezone.utc).isoformat()
    preview = post.body[:120].replace("\n", " ")
    if len(post.body) > 120:
        preview += "..."
    return (
        f"id={post.id} score={post.score} author={post.author} created={created}\n"
        f"  title: {post.title}\n"
        f"  url:   {post.url}\n"
        f"  body:  {preview}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List eligible r/AmItheAsshole posts for the video pipeline.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=FETCH_LIMIT,
        help=f"Number of top-day posts to examine (default: {FETCH_LIMIT})",
    )
    args = parser.parse_args()

    settings = get_settings()
    _validate_reddit_config(settings)

    from app.database import get_db_session, init_db

    init_db()

    with get_db_session() as db:
        posts = fetch_eligible_posts(db, settings=settings, limit=args.limit)

    if not posts:
        print("No eligible posts found.")
        return

    print(f"Found {len(posts)} eligible post(s):\n")
    for index, post in enumerate(posts, start=1):
        print(f"{index}. {_format_post(post)}\n")


if __name__ == "__main__":
    main()
