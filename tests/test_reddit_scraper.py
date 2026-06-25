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


# --- _submission_to_dict / _fetch_listing (PRAW) ----------------------------

class FakeAuthor:
    def __init__(self, name):
        self.name = name


class FakeSubmission:
    def __init__(self, **kw):
        self.id = kw.get("id", "x")
        self.title = kw.get("title", "T")
        self.selftext = kw.get("selftext", "body")
        self.score = kw.get("score", 100)
        self.stickied = kw.get("stickied", False)
        self.over_18 = kw.get("over_18", False)
        self.created_utc = kw.get("created_utc", 0.0)
        self.author = kw.get("author", FakeAuthor("u"))
        self.permalink = kw.get("permalink", "/r/x/1/")


class FakeSubreddit:
    def __init__(self, subs):
        self._subs = subs
        self.called_with = None

    def top(self, time_filter="day", limit=50):
        self.called_with = (time_filter, limit)
        return list(self._subs)[:limit]


class FakeReddit:
    def __init__(self, subs):
        self._sr = FakeSubreddit(subs)

    def subreddit(self, name):
        return self._sr


def test_submission_to_dict_handles_deleted_author():
    d = rs._submission_to_dict(FakeSubmission(id="a", author=None))
    assert d["author"] is None
    d2 = rs._submission_to_dict(FakeSubmission(id="b", author=FakeAuthor("alice")))
    assert d2["author"] == "alice"


def test_fetch_listing_maps_submissions(monkeypatch):
    subs = [FakeSubmission(id="a", title="A"), FakeSubmission(id="b", title="B")]
    monkeypatch.setattr(rs, "_reddit_client", lambda s: FakeReddit(subs))

    out = rs._fetch_listing(Settings(subreddit_name="AmItheAsshole"), 50)
    assert [p["id"] for p in out] == ["a", "b"]
    assert out[0]["title"] == "A"


def test_reddit_client_requires_credentials():
    with pytest.raises(ValueError, match="Reddit API credentials"):
        rs._reddit_client(Settings(reddit_client_id="", reddit_client_secret=""))


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
