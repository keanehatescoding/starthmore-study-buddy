"""Basic stats for the demo: accuracy, streak, queue sizes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from app.grade import _aware, scoped_items, user_zone
from app.models import QuizItem, ReviewLog, ReviewState
from app.srs import local_day_start


def compute_stats(session: Session, user_id, tz=None, now: datetime | None = None) -> dict:
    """Answered/accuracy/streak count every answer in review_logs, not just
    each item's latest; streak days are local dates in `tz` (the user's zone)."""
    if tz is None:
        tz = user_zone(session, user_id)
    by_verdict = dict(session.exec(
        select(ReviewLog.verdict, func.count(ReviewLog.id))
        .where(ReviewLog.user_id == user_id)
        .group_by(ReviewLog.verdict)
    ).all())
    answered = sum(by_verdict.values())
    items_total = session.exec(
        scoped_items(user_id).with_only_columns(func.count(QuizItem.id))
    ).one()
    lapses = session.exec(
        select(func.coalesce(func.sum(ReviewState.lapses), 0))
        .where(ReviewState.user_id == user_id)
    ).one()

    return {
        "answered": answered,
        "accuracy": round(by_verdict.get("correct", 0) / answered, 3) if answered else None,
        "streak_days": _streak(session, user_id, tz, now or datetime.now(timezone.utc)),
        "items_total": items_total,
        "lapses": lapses,
    }


STREAK_WINDOW_DAYS = 32


def _streak(session: Session, user_id, tz, now: datetime) -> int:
    """Consecutive local days with an answer, ending today (or yesterday).

    Reads answer times one window of local days at a time, newest first,
    doubling the window while the streak runs past it, so a long history
    isn't loaded just to find this month's streak."""
    today = now.astimezone(tz).date()
    days: set = set()
    loaded = 0  # local days read so far, counting back from today
    span = STREAK_WINDOW_DAYS
    while True:
        days |= {
            _aware(at).astimezone(tz).date()
            for at in session.exec(
                select(ReviewLog.answered_at).where(
                    ReviewLog.user_id == user_id,
                    ReviewLog.answered_at >= local_day_start(now, tz, 1 - loaded - span),
                    ReviewLog.answered_at < local_day_start(now, tz, 1 - loaded),
                )
            )
        }
        loaded += span
        cursor = today if today in days else today - timedelta(days=1)
        streak = 0
        while cursor in days:
            streak += 1
            cursor -= timedelta(days=1)
        if (today - cursor).days < loaded:
            return streak  # stopped on a day that was read: the streak is complete
        span *= 2
