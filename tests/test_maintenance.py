from __future__ import annotations

from datetime import datetime, timedelta

from app.models import UsedPost
from app import maintenance as mt


def add(db, reddit_id, status, *, created_at=None, youtube_video_id=None):
    row = UsedPost(
        reddit_id=reddit_id, title="t", upvotes=1, status=status,
        youtube_video_id=youtube_video_id,
    )
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    return row


# --- reset_failed_uploads ----------------------------------------------------

def test_reset_failed_uploads_deletes_only_failed(db_session):
    add(db_session, "fail1", "upload_failed")
    add(db_session, "fail2", "upload_failed")
    add(db_session, "done", "completed", youtube_video_id="Y1")
    add(db_session, "claimed", "uploading")
    db_session.commit()

    count = mt.reset_failed_uploads(db_session)
    assert count == 2

    remaining = {r.reddit_id for r in db_session.query(UsedPost).all()}
    assert remaining == {"done", "claimed"}  # completed + uploading untouched


def test_reset_failed_uploads_noop_when_none(db_session):
    add(db_session, "done", "completed")
    db_session.commit()
    assert mt.reset_failed_uploads(db_session) == 0
    assert db_session.query(UsedPost).count() == 1


# --- find_stuck_uploads ------------------------------------------------------

def test_find_stuck_uploads_returns_old_uploading_only(db_session):
    now = datetime.utcnow()
    add(db_session, "old_stuck", "uploading", created_at=now - timedelta(hours=3))
    add(db_session, "recent", "uploading", created_at=now - timedelta(minutes=2))
    add(db_session, "old_failed", "upload_failed", created_at=now - timedelta(hours=3))
    add(db_session, "old_done", "completed", created_at=now - timedelta(hours=3))
    db_session.commit()

    stuck = mt.find_stuck_uploads(db_session, older_than_minutes=60)
    assert [r.reddit_id for r in stuck] == ["old_stuck"]


def test_find_stuck_uploads_respects_threshold(db_session):
    now = datetime.utcnow()
    add(db_session, "u", "uploading", created_at=now - timedelta(minutes=30))
    db_session.commit()

    assert mt.find_stuck_uploads(db_session, older_than_minutes=60) == []
    assert [r.reddit_id for r in mt.find_stuck_uploads(db_session, older_than_minutes=10)] == ["u"]
