from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from app.database import get_db
from app.models import UsedPost
from app.pipeline import PipelineResult


@pytest.fixture
def client(db_session):
    """TestClient with get_db overridden to the in-memory session.

    Not used as a context manager, so the app lifespan (validate_required +
    scheduler) does not run — endpoints are exercised in isolation.
    """
    main.app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def seed(db_session):
    db_session.add_all([
        UsedPost(
            reddit_id="old", title="Old", upvotes=100, status="completed",
            youtube_video_id="OLD1",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        UsedPost(
            reddit_id="new", title="New", upvotes=200, status="completed",
            created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        ),
    ])
    db_session.commit()


# --- /health -----------------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# --- /posts ------------------------------------------------------------------

def test_list_posts_orders_desc_and_computes_url(client, db_session):
    seed(db_session)
    r = client.get("/posts")
    assert r.status_code == 200
    body = r.json()
    assert [p["reddit_id"] for p in body] == ["new", "old"]  # created_at desc
    old = next(p for p in body if p["reddit_id"] == "old")
    new = next(p for p in body if p["reddit_id"] == "new")
    assert old["youtube_url"] == "https://www.youtube.com/watch?v=OLD1"
    assert new["youtube_url"] is None


def test_list_posts_pagination(client, db_session):
    seed(db_session)
    r = client.get("/posts", params={"skip": 1, "limit": 1})
    assert [p["reddit_id"] for p in r.json()] == ["old"]


def test_list_posts_validates_query_bounds(client):
    assert client.get("/posts", params={"limit": 0}).status_code == 422
    assert client.get("/posts", params={"limit": 101}).status_code == 422
    assert client.get("/posts", params={"skip": -1}).status_code == 422


# --- /posts/{reddit_id} ------------------------------------------------------

def test_get_post_found(client, db_session):
    seed(db_session)
    r = client.get("/posts/old")
    assert r.status_code == 200
    assert r.json()["youtube_url"] == "https://www.youtube.com/watch?v=OLD1"


def test_get_post_not_found(client):
    r = client.get("/posts/missing")
    assert r.status_code == 404
    assert r.json()["detail"] == "Post not found"


# --- /pipeline/run -----------------------------------------------------------

def test_trigger_pipeline_success(client, monkeypatch):
    result = PipelineResult(
        success=True, reddit_id="r1",
        upload=type("U", (), {"youtube_url": "https://youtu.be/r1"})(),
        ready_path="/out/ready/r1.mp4",
        video=type("V", (), {"video_path": "/out/r1/final.mp4"})(),
    )
    monkeypatch.setattr(main, "run_pipeline_locked", lambda db: result)

    r = client.post("/pipeline/run")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["reddit_id"] == "r1"
    assert body["youtube_url"] == "https://youtu.be/r1"
    assert body["ready_path"] == "/out/ready/r1.mp4"
    assert body["video_path"] == "/out/r1/final.mp4"


def test_trigger_pipeline_skipped(client, monkeypatch):
    monkeypatch.setattr(
        main, "run_pipeline_locked",
        lambda db: PipelineResult(skipped=True, reason="already_running"),
    )
    r = client.post("/pipeline/run")
    assert r.status_code == 200
    body = r.json()
    assert body["skipped"] is True
    assert body["reason"] == "already_running"
    assert body["youtube_url"] is None
