from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.database import get_db_session
from app.pipeline import run_pipeline_locked
from app.utils.logging import get_logger

logger = get_logger(__name__)

scheduler = BackgroundScheduler()


def run_pipeline_job() -> None:
    settings = get_settings()
    try:
        settings.validate_required()
    except ValueError as exc:
        logger.error("Scheduled pipeline skipped: %s", exc)
        return

    logger.info("Scheduled pipeline run starting")
    with get_db_session() as db:
        result = run_pipeline_locked(db, settings=settings)

    if result.skipped:
        logger.info("Scheduled pipeline skipped: %s", result.reason)
    elif result.success:
        logger.info(
            "Scheduled pipeline succeeded for post %s",
            result.reddit_id,
        )
    else:
        logger.error(
            "Scheduled pipeline failed for post %s: %s",
            result.reddit_id,
            result.reason,
        )


def start_scheduler() -> None:
    if scheduler.running:
        return

    settings = get_settings()
    scheduler.add_job(
        run_pipeline_job,
        trigger=CronTrigger(
            hour="9,18",
            minute=0,
            timezone=settings.scheduler_timezone,
        ),
        id="aita_pipeline",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started: 9:00 and 18:00 %s",
        settings.scheduler_timezone,
    )


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
