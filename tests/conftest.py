from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base


@pytest.fixture
def db_session():
    """A fresh in-memory SQLite session with all tables created."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    session: Session = maker()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def recent_ts(hours_ago: float = 1.0) -> float:
    """A unix timestamp `hours_ago` hours before now (UTC)."""
    return datetime.now(timezone.utc).timestamp() - hours_ago * 3600


@dataclass
class FakeAuthor:
    name: str


@dataclass
class FakeSubmission:
    """Duck-typed stand-in for praw.models.Submission."""

    id: str
    title: str = "An AITA story title that is reasonably descriptive"
    selftext: str = ""
    score: int = 1000
    stickied: bool = False
    created_utc: float = field(default_factory=recent_ts)
    author: Optional[FakeAuthor] = field(default_factory=lambda: FakeAuthor("someuser"))
    permalink: str = "/r/AmItheAsshole/comments/abc123/some_post/"


class FakeSubreddit:
    def __init__(self, submissions):
        self._submissions = submissions

    def top(self, time_filter="day", limit=50):
        return list(self._submissions)[:limit]


class FakeReddit:
    def __init__(self, submissions):
        self._subreddit = FakeSubreddit(submissions)

    def subreddit(self, name):
        return self._subreddit
