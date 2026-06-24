from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, computed_field


class UsedPostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reddit_id: str
    title: str
    upvotes: int
    script_word_count: Optional[int]
    video_path: Optional[str]
    youtube_video_id: Optional[str]
    status: str
    created_at: datetime
    processed_at: Optional[datetime]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def youtube_url(self) -> Optional[str]:
        if self.youtube_video_id:
            return f"https://www.youtube.com/watch?v={self.youtube_video_id}"
        return None


class HealthResponse(BaseModel):
    status: str = "ok"


class PipelineRunResponse(BaseModel):
    skipped: bool = False
    success: bool = False
    reason: str = ""
    reddit_id: Optional[str] = None
    youtube_url: Optional[str] = None
    ready_path: Optional[str] = None
    video_path: Optional[str] = None
