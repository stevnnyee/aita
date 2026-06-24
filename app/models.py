from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UsedPost(Base):
    __tablename__ = "used_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    reddit_id: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    upvotes: Mapped[int] = mapped_column(Integer)
    script_word_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    video_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    youtube_video_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
