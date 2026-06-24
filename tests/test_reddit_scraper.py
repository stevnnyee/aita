from __future__ import annotations

import pytest

from app.config import Settings
from app.models import UsedPost
from app.services import reddit_scraper as rs

from tests.conftest import FakeAuthor, FakeReddit, FakeSubmission, recent_ts

CUTOFF = recent_ts(hours_ago=24)
LONG_TITLE = "x" * rs.MIN_TITLE_ONLY_CHARS
GOOD_BODY = "y" * (rs.MIN_BODY_CHARS + 10)


# --- _extract_body -----------------------------------------------------------

def test_extract_body_returns_selftext():
    sub = FakeSubmission(id="a", selftext="  real story body  ")
    assert rs._extract_body(sub) == "real story body"


@pytest.mark.parametrize("marker", ["[removed]", "[deleted]", "[REMOVED]"])
def test_extract_body_rejects_removed_selftext(marker):
    assert rs._extract_body(FakeSubmission(id="a", selftext=marker)) is None


def test_extract_body_falls_back_to_long_title():
    sub = FakeSubmission(id="a", title=LONG_TITLE, selftext="")
    assert rs._extract_body(sub) == LONG_TITLE


def test_extract_body_rejects_short_title_only_post():
    sub = FakeSubmission(id="a", title="too short", selftext="")
    assert rs._extract_body(sub) is None


# --- _author_name ------------------------------------------------------------

def test_author_name_handles_deleted_author():
    assert rs._author_name(FakeSubmission(id="a", author=None)) == "[deleted]"


def test_author_name_returns_username():
    sub = FakeSubmission(id="a", author=FakeAuthor("alice"))
    assert rs._author_name(sub) == "alice"


# --- _is_eligible_submission -------------------------------------------------

def _eligible(sub, *, used=None):
    return rs._is_eligible_submission(
        sub,
        min_upvotes=500,
        cutoff_utc=CUTOFF,
        used_ids=used or set(),
    )


def test_eligible_happy_path():
    assert _eligible(FakeSubmission(id="a", selftext=GOOD_BODY)) is True


def test_eligible_rejects_used():
    sub = FakeSubmission(id="a", selftext=GOOD_BODY)
    assert _eligible(sub, used={"a"}) is False


def test_eligible_rejects_stickied():
    assert _eligible(FakeSubmission(id="a", selftext=GOOD_BODY, stickied=True)) is False


def test_eligible_rejects_low_score():
    assert _eligible(FakeSubmission(id="a", selftext=GOOD_BODY, score=499)) is False


def test_eligible_rejects_old_post():
    sub = FakeSubmission(id="a", selftext=GOOD_BODY, created_utc=recent_ts(48))
    assert _eligible(sub) is False


def test_eligible_rejects_short_body():
    assert _eligible(FakeSubmission(id="a", selftext="short")) is False


# --- _to_reddit_post ---------------------------------------------------------

def test_to_reddit_post_maps_fields():
    sub = FakeSubmission(id="abc", title=" Title ", selftext=GOOD_BODY, score=777)
    post = rs._to_reddit_post(sub)
    assert post.id == "abc"
    assert post.title == "Title"
    assert post.body == GOOD_BODY
    assert post.score == 777
    assert post.url == "https://www.reddit.com" + sub.permalink
    assert post.author == "someuser"
    assert isinstance(post.created_utc, float)


def test_to_reddit_post_raises_without_body():
    with pytest.raises(ValueError):
        rs._to_reddit_post(FakeSubmission(id="a", title="short", selftext=""))


# --- fetch_eligible_posts (integration with DB + fake reddit) ----------------

@pytest.fixture
def settings():
    return Settings(
        reddit_client_id="id",
        reddit_client_secret="secret",
        min_upvotes=500,
    )


def test_fetch_eligible_posts_filters_and_dedupes(db_session, settings, monkeypatch):
    # "b" already used; "stick" stickied; "low" under threshold; "ok1"/"ok2" pass.
    db_session.add(UsedPost(reddit_id="b", title="t", upvotes=900))
    db_session.commit()

    subs = [
        FakeSubmission(id="ok1", selftext=GOOD_BODY, score=900),
        FakeSubmission(id="b", selftext=GOOD_BODY, score=900),
        FakeSubmission(id="stick", selftext=GOOD_BODY, stickied=True),
        FakeSubmission(id="low", selftext=GOOD_BODY, score=10),
        FakeSubmission(id="ok2", selftext=GOOD_BODY, score=800),
    ]
    monkeypatch.setattr(rs, "_reddit_client", lambda s: FakeReddit(subs))

    posts = rs.fetch_eligible_posts(db_session, settings=settings)

    assert [p.id for p in posts] == ["ok1", "ok2"]


def test_fetch_eligible_post_returns_first(db_session, settings, monkeypatch):
    subs = [
        FakeSubmission(id="ok1", selftext=GOOD_BODY, score=900),
        FakeSubmission(id="ok2", selftext=GOOD_BODY, score=800),
    ]
    monkeypatch.setattr(rs, "_reddit_client", lambda s: FakeReddit(subs))

    assert rs.fetch_eligible_post(db_session, settings=settings).id == "ok1"


def test_fetch_eligible_post_returns_none_when_empty(db_session, settings, monkeypatch):
    monkeypatch.setattr(rs, "_reddit_client", lambda s: FakeReddit([]))
    assert rs.fetch_eligible_post(db_session, settings=settings) is None


# --- _validate_reddit_config -------------------------------------------------

def test_validate_reddit_config_raises_when_missing():
    with pytest.raises(ValueError) as exc:
        rs._validate_reddit_config(Settings(reddit_client_id="", reddit_client_secret=""))
    assert "reddit_client_id" in str(exc.value)


def test_validate_reddit_config_passes_when_present():
    rs._validate_reddit_config(Settings(reddit_client_id="id", reddit_client_secret="sec"))
