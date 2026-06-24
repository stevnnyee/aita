from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.models import UsedPost
from app.services import reddit_scraper as rs


def recent_ts(hours_ago: float = 1.0) -> float:
    return datetime.now(timezone.utc).timestamp() - hours_ago * 3600


CUTOFF = recent_ts(hours_ago=24)
LONG_TITLE = "x" * rs.MIN_TITLE_ONLY_CHARS
GOOD_BODY = "y" * (rs.MIN_BODY_CHARS + 10)


def post(**overrides):
    """A Reddit JSON post-data dict with sensible defaults."""
    data = {
        "id": "abc",
        "title": "A reasonably descriptive AITA title for testing purposes",
        "selftext": GOOD_BODY,
        "score": 1000,
        "stickied": False,
        "over_18": False,
        "created_utc": recent_ts(1),
        "author": "someuser",
        "permalink": "/r/AmItheAsshole/comments/abc/some_post/",
    }
    data.update(overrides)
    return data


# --- _extract_body -----------------------------------------------------------

def test_extract_body_returns_selftext():
    assert rs._extract_body(post(selftext="  real story body  ")) == "real story body"


@pytest.mark.parametrize("marker", ["[removed]", "[deleted]", "[REMOVED]"])
def test_extract_body_rejects_removed_selftext(marker):
    assert rs._extract_body(post(selftext=marker)) is None


def test_extract_body_falls_back_to_long_title():
    assert rs._extract_body(post(title=LONG_TITLE, selftext="")) == LONG_TITLE


def test_extract_body_rejects_short_title_only_post():
    assert rs._extract_body(post(title="too short", selftext="")) is None


# --- _author_name / _submission_url ------------------------------------------

def test_author_name_handles_deleted_author():
    assert rs._author_name(post(author=None)) == "[deleted]"
    assert rs._author_name(post(author="alice")) == "alice"


def test_submission_url_prefixes_domain():
    assert rs._submission_url(post(permalink="/r/x/1/")) == "https://www.reddit.com/r/x/1/"


# --- _is_eligible_submission -------------------------------------------------

def _eligible(p, *, used=None):
    return rs._is_eligible_submission(
        p, min_upvotes=500, cutoff_utc=CUTOFF, used_ids=used or set()
    )


def test_eligible_happy_path():
    assert _eligible(post()) is True


def test_eligible_rejects_used():
    assert _eligible(post(id="abc"), used={"abc"}) is False


def test_eligible_rejects_stickied():
    assert _eligible(post(stickied=True)) is False


def test_eligible_rejects_nsfw():
    assert _eligible(post(over_18=True)) is False


def test_eligible_rejects_low_score():
    assert _eligible(post(score=499)) is False


def test_eligible_rejects_old_post():
    assert _eligible(post(created_utc=recent_ts(48))) is False


def test_eligible_rejects_short_body():
    assert _eligible(post(selftext="short")) is False


# --- _to_reddit_post ---------------------------------------------------------

def test_to_reddit_post_maps_fields():
    rp = rs._to_reddit_post(post(id="abc", title=" Title ", selftext=GOOD_BODY, score=777))
    assert rp.id == "abc"
    assert rp.title == "Title"
    assert rp.body == GOOD_BODY
    assert rp.score == 777
    assert rp.url.startswith("https://www.reddit.com/")
    assert rp.author == "someuser"


def test_to_reddit_post_raises_without_body():
    with pytest.raises(ValueError):
        rs._to_reddit_post(post(title="short", selftext=""))


# --- _fetch_listing (JSON parsing) ------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_listing_parses_children(monkeypatch):
    payload = {
        "data": {
            "children": [
                {"kind": "t3", "data": {"id": "a", "title": "A"}},
                {"kind": "t3", "data": {"id": "b", "title": "B"}},
                {"kind": "t1", "data": {"id": "c"}},  # not a post -> skipped
            ]
        }
    }
    captured = {}

    def fake_get(url, headers, params, timeout, **kwargs):
        captured.update(url=url, headers=headers, params=params)
        return FakeResponse(payload)

    monkeypatch.setattr(rs.httpx, "get", fake_get)

    out = rs._fetch_listing(Settings(subreddit_name="AmItheAsshole"), 50)
    assert [p["id"] for p in out] == ["a", "b"]
    assert "AmItheAsshole" in captured["url"]
    assert captured["headers"]["User-Agent"]  # UA is sent
    assert captured["params"]["t"] == "day"


# --- fetch_eligible_posts (filtering + DB dedupe) ---------------------------

@pytest.fixture
def settings():
    return Settings(min_upvotes=500)


def test_fetch_eligible_posts_filters_and_dedupes(db_session, settings, monkeypatch):
    db_session.add(UsedPost(reddit_id="b", title="t", upvotes=900))
    db_session.commit()

    listing = [
        post(id="ok1", score=900),
        post(id="b", score=900),
        post(id="stick", stickied=True),
        post(id="low", score=10),
        post(id="ok2", score=800),
    ]
    monkeypatch.setattr(rs, "_fetch_listing", lambda s, limit: listing)

    posts = rs.fetch_eligible_posts(db_session, settings=settings)
    assert [p.id for p in posts] == ["ok1", "ok2"]


def test_fetch_eligible_post_returns_first(db_session, settings, monkeypatch):
    monkeypatch.setattr(
        rs, "_fetch_listing",
        lambda s, limit: [post(id="ok1", score=900), post(id="ok2", score=800)],
    )
    assert rs.fetch_eligible_post(db_session, settings=settings).id == "ok1"


def test_fetch_eligible_post_returns_none_when_empty(db_session, settings, monkeypatch):
    monkeypatch.setattr(rs, "_fetch_listing", lambda s, limit: [])
    assert rs.fetch_eligible_post(db_session, settings=settings) is None
