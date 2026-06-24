from __future__ import annotations

from app.config import Settings, get_settings
from app.models import Base, UsedPost


def test_get_settings_is_pure_and_cached(tmp_path, monkeypatch):
    # get_settings() must not create directories as a side effect.
    monkeypatch.chdir(tmp_path)
    settings = get_settings()
    assert get_settings() is settings  # lru_cache
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "output").exists()


def test_ensure_dirs_creates_all_dirs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Settings()
    s.ensure_dirs()
    for d in ("logs", "output", "assets/backgrounds", "assets/music", "credentials"):
        assert (tmp_path / d).is_dir()
    # both youtube file parents, not just client_secrets
    assert s.youtube_token_file.parent.is_dir()


def test_validate_required_reports_missing():
    s = Settings(elevenlabs_api_key="", openai_api_key="x")
    try:
        s.validate_required()
        assert False, "expected ValueError"
    except ValueError as e:
        assert "elevenlabs_api_key" in str(e)
        assert "openai_api_key" not in str(e)  # this one is set


def test_metadata_create_all_round_trips(db_session):
    db_session.add(UsedPost(reddit_id="abc", title="t", upvotes=900))
    db_session.commit()
    rows = db_session.query(UsedPost).all()
    assert len(rows) == 1
    assert rows[0].status == "pending"  # default applied
    assert rows[0].created_at is not None  # server_default populated & readable
