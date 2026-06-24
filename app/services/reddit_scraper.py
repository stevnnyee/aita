from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import UsedPost
from app.utils.logging import get_logger

logger = get_logger(__name__)

REMOVED_MARKERS = {"[removed]", "[deleted]"}
MIN_BODY_CHARS = 50
MIN_TITLE_ONLY_CHARS = 100
FETCH_LIMIT = 50
REQUEST_TIMEOUT = 30.0

# Reddit's public JSON listing — no API key required, just a descriptive
# User-Agent. Read-only and rate-limited, which is plenty for a twice-daily run.
LISTING_URL = "https://www.reddit.com/r/{subreddit}/top.json"


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


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch_listing(settings: Settings, limit: int) -> list[dict[str, Any]]:
    """Fetch the subreddit's top-of-day posts as a list of post data dicts."""
    url = LISTING_URL.format(subreddit=settings.subreddit_name)
    # A browser-like User-Agent helps avoid Reddit's bot block on the public
    # JSON endpoint. follow_redirects handles the occasional www->old redirect.
    params = {"t": "day", "limit": limit, "raw_json": 1}

    response = httpx.get(
        url,
        headers=_BROWSER_HEADERS,
        params=params,
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()

    children = payload.get("data", {}).get("children", [])
    return [
        child.get("data", {})
        for child in children
        if child.get("kind") == "t3" and isinstance(child.get("data"), dict)
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
