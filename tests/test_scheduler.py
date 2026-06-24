from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app import scheduler as sch
from app.pipeline import PipelineResult


@contextmanager
def fake_db_session():
    yield "DB"


@pytest.fixture
def patched(monkeypatch):
    """Make get_db_session a no-op CM and settings.validate_required pass."""
    monkeypatch.setattr(sch, "get_db_session", fake_db_session)
    monkeypatch.setattr(
        sch, "get_settings", lambda: SimpleNamespace(validate_required=lambda: None)
    )


def test_job_runs_pipeline_on_success(patched, monkeypatch):
    seen = {}

    def fake(db, settings=None):
        seen["db"] = db
        return PipelineResult(success=True, reddit_id="r1")

    monkeypatch.setattr(sch, "run_pipeline_locked", fake)
    sch.run_pipeline_job()
    assert seen["db"] == "DB"


def test_job_skips_when_validation_fails(monkeypatch):
    def bad_settings():
        return SimpleNamespace(
            validate_required=lambda: (_ for _ in ()).throw(ValueError("no keys"))
        )

    monkeypatch.setattr(sch, "get_settings", bad_settings)
    called = {"ran": False}
    monkeypatch.setattr(
        sch, "run_pipeline_locked",
        lambda *a, **k: called.__setitem__("ran", True),
    )
    monkeypatch.setattr(sch, "get_db_session", fake_db_session)

    sch.run_pipeline_job()
    assert called["ran"] is False  # pipeline never invoked


@pytest.mark.parametrize(
    "result",
    [
        PipelineResult(skipped=True, reason="no_eligible_posts"),
        PipelineResult(success=True, reddit_id="r1"),
        PipelineResult(success=False, reddit_id="r1", reason="boom"),
    ],
)
def test_job_handles_all_result_kinds(patched, monkeypatch, result):
    monkeypatch.setattr(sch, "run_pipeline_locked", lambda db, settings=None: result)
    sch.run_pipeline_job()  # must not raise for any result kind


def test_start_and_stop_scheduler_registers_job():
    try:
        sch.start_scheduler()
        job = sch.scheduler.get_job("aita_pipeline")
        assert job is not None
        sch.start_scheduler()  # idempotent: already running, no error
    finally:
        sch.stop_scheduler()
    assert not sch.scheduler.running
