from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import UsedPost
from app.utils.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    import praw
    from praw.models import Submission

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


def _used_reddit_ids(db: Session) -> set[str]:
    return set(db.scalars(select(UsedPost.reddit_id)).all())


def _reddit_client(settings: Settings) -> "praw.Reddit":
    """Read-only PRAW client (app-only OAuth).

    Authenticated requests go through oauth.reddit.com, which Reddit allows from
    datacenter IPs — unlike the unauthenticated public JSON endpoint, which is
    blocked from cloud hosts like EC2.
    """
    import praw

    if not settings.reddit_client_id or not settings.reddit_client_secret:
        raise ValueError(
            "Missing Reddit API credentials. Create a 'script' app at "
            "https://www.reddit.com/prefs/apps and set REDDIT_CLIENT_ID / "
            "REDDIT_CLIENT_SECRET in .env."
        )

    return praw.Reddit(
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        user_agent=settings.reddit_user_agent,
    )


def _submission_to_dict(submission: "Submission") -> dict[str, Any]:
    """Flatten the fields we care about so the rest of the module stays simple."""
    author = getattr(submission, "author", None)
    return {
        "id": submission.id,
        "title": submission.title,
        "selftext": submission.selftext,
        "score": submission.score,
        "stickied": submission.stickied,
        "over_18": submission.over_18,
        "created_utc": submission.created_utc,
        "author": author.name if author is not None else None,
        "permalink": submission.permalink,
    }


def _fetch_listing(settings: Settings, limit: int) -> list[dict[str, Any]]:
    """Fetch the subreddit's top-of-day posts as a list of post data dicts."""
    reddit = _reddit_client(settings)
    subreddit = reddit.subreddit(settings.subreddit_name)
    return [
        _submission_to_dict(submission)
        for submission in subreddit.top(time_filter="day", limit=limit)
    ]


def _submission_url(post: dict[str, Any]) -> str:
    return f"https://www.reddit.com{post.get('permalink', '')}"


def _author_name(post: dict[str, Any]) -> str:
    return str(post.get("author") or "[deleted]")


def _extract_body(post: dict[str, Any]) -> Optional[str]:
    title = (post.get("title") or "").strip()
    selftext = (post.get("selftext") or "").strip()

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
    post: dict[str, Any],
    *,
    min_upvotes: int,
    cutoff_utc: float,
    used_ids: set[str],
) -> bool:
    if post.get("id") in used_ids:
        return False

    if post.get("stickied"):
        return False

    if post.get("over_18"):
        return False

    if int(post.get("score", 0)) < min_upvotes:
        return False

    if float(post.get("created_utc", 0.0)) < cutoff_utc:
        return False

    body = _extract_body(post)
    if body is None or len(body) < MIN_BODY_CHARS:
        return False

    return True


def _to_reddit_post(post: dict[str, Any]) -> RedditPost:
    body = _extract_body(post)
    if body is None:
        raise ValueError(f"Submission {post.get('id')} has no usable body")

    return RedditPost(
        id=str(post["id"]),
        title=(post.get("title") or "").strip(),
        body=body,
        score=int(post.get("score", 0)),
        url=_submission_url(post),
        author=_author_name(post),
        created_utc=float(post.get("created_utc", 0.0)),
    )


def fetch_eligible_posts(
    db: Session,
    *,
    settings: Optional[Settings] = None,
    limit: int = FETCH_LIMIT,
) -> list[RedditPost]:
    """Return all eligible top posts, highest score first."""
    settings = settings or get_settings()

    cutoff_utc = (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()
    used_ids = _used_reddit_ids(db)

    posts = _fetch_listing(settings, limit)
    eligible: list[RedditPost] = []
    examined = 0

    for post in posts:
        examined += 1
        if not _is_eligible_submission(
            post,
            min_upvotes=settings.min_upvotes,
            cutoff_utc=cutoff_utc,
            used_ids=used_ids,
        ):
            continue
        eligible.append(_to_reddit_post(post))

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
