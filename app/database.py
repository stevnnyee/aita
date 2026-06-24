from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

settings = get_settings()

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    echo=False,
)

# expire_on_commit=False keeps attributes readable after commit/close, so ORM
# objects returned from get_db_session() don't raise DetachedInstanceError when
# their fields (e.g. the server-defaulted created_at) are accessed afterwards.
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


def init_db() -> None:
    settings.ensure_dirs()
    Base.metadata.create_all(bind=engine)


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Write-oriented session: commits on success, rolls back on error.

    Use this for pipeline/background work that mutates the database.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for database sessions.

    Does NOT auto-commit: a commit here runs during request teardown, after the
    response is built, so a failure would turn a successful request into a 500
    and read-only endpoints would commit needlessly. Handlers that write must
    call ``session.commit()`` explicitly.
    """
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
