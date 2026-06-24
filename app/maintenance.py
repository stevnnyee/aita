from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import UsedPost
from app.utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_STUCK_MINUTES = 60


def reset_failed_uploads(db: Session) -> int:
    """Delete rows whose upload raised an error so those posts can be retried.

    ``upload_failed`` means ``upload()`` threw before completing, so re-running
    is safe. ``uploading`` rows are intentionally NOT touched here (see
    ``find_stuck_uploads``): the video may already be live on YouTube, and
    deleting the row would let the post be re-selected and uploaded twice.
    """
    rows = db.scalars(
        select(UsedPost).where(UsedPost.status == "upload_failed")
    ).all()
    count = len(rows)
    for row in rows:
        db.delete(row)
    db.commit()
    if count:
        logger.info("Reset %d failed upload(s) for retry", count)
    return count


def find_stuck_uploads(
    db: Session, *, older_than_minutes: int = DEFAULT_STUCK_MINUTES
) -> list[UsedPost]:
    """Return ``uploading`` rows older than the threshold, for MANUAL review.

    These were claimed but never confirmed completed (process crashed, or the
    completion commit failed after a successful upload). Because the video may
    or may not have been published, they are reported rather than auto-deleted:
    check YouTube, then either delete the row to retry, or set
    ``status='completed'`` with the real ``youtube_video_id``.

    ``created_at`` is stored as naive UTC (DB ``server_default``), so the cutoff
    is computed in naive UTC to match.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=older_than_minutes)
    return list(
        db.scalars(
            select(UsedPost)
            .where(UsedPost.status == "uploading", UsedPost.created_at < cutoff)
            .order_by(UsedPost.created_at)
        ).all()
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Maintenance for the AITA pipeline database.",
    )
    parser.add_argument(
        "--reset-failed",
        action="store_true",
        help="Delete 'upload_failed' rows so those posts can be retried",
    )
    parser.add_argument(
        "--list-stuck",
        action="store_true",
        help="List orphaned 'uploading' rows that need manual review",
    )
    parser.add_argument(
        "--stuck-minutes",
        type=int,
        default=DEFAULT_STUCK_MINUTES,
        help=f"Age threshold for stuck uploads (default: {DEFAULT_STUCK_MINUTES})",
    )
    args = parser.parse_args()

    if not args.reset_failed and not args.list_stuck:
        raise SystemExit("Use --reset-failed and/or --list-stuck.")

    from app.database import get_db_session, init_db

    init_db()
    with get_db_session() as db:
        if args.reset_failed:
            count = reset_failed_uploads(db)
            print(f"Reset {count} failed upload(s) for retry.")

        if args.list_stuck:
            stuck = find_stuck_uploads(db, older_than_minutes=args.stuck_minutes)
            if not stuck:
                print(f"No uploads stuck longer than {args.stuck_minutes} min.")
            else:
                print(f"{len(stuck)} stuck upload(s) needing manual review:")
                for row in stuck:
                    print(
                        f"  reddit_id={row.reddit_id} created_at={row.created_at} "
                        f"video_path={row.video_path}"
                    )
                print(
                    "Check YouTube for each, then delete the row to retry or set "
                    "status='completed' with the real youtube_video_id."
                )


if __name__ == "__main__":
    main()
