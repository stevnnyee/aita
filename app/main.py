from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db, init_db
from app.models import UsedPost
from app.pipeline import run_pipeline_locked
from app.scheduler import start_scheduler, stop_scheduler
from app.schemas import HealthResponse, PipelineRunResponse, UsedPostRead
from app.utils.logging import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    settings = get_settings()
    settings.validate_required()
    init_db()
    start_scheduler()
    logger.info("AITA pipeline API started")
    yield
    stop_scheduler()
    logger.info("AITA pipeline API stopped")


app = FastAPI(title="AITA Video Pipeline", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.post("/pipeline/run", response_model=PipelineRunResponse)
def trigger_pipeline(db: Session = Depends(get_db)) -> PipelineRunResponse:
    result = run_pipeline_locked(db)
    return PipelineRunResponse(
        skipped=result.skipped,
        success=result.success,
        reason=result.reason,
        reddit_id=result.reddit_id,
        youtube_url=result.upload.youtube_url if result.upload else None,
        ready_path=str(result.ready_path) if result.ready_path else None,
        video_path=(
            str(result.video.video_path) if result.video else None
        ),
    )


@app.get("/posts", response_model=list[UsedPostRead])
def list_posts(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[UsedPostRead]:
    posts = db.scalars(
        select(UsedPost)
        .order_by(UsedPost.created_at.desc())
        .offset(skip)
        .limit(limit)
    ).all()
    return list(posts)


@app.get("/posts/{reddit_id}", response_model=UsedPostRead)
def get_post(reddit_id: str, db: Session = Depends(get_db)) -> UsedPostRead:
    post: Optional[UsedPost] = db.scalar(
        select(UsedPost).where(UsedPost.reddit_id == reddit_id)
    )
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return post
