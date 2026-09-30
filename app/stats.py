"""Basic stats for the demo: accuracy, streak, queue sizes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, func, select

from app.grade import _aware, scoped_items
from app.models import QuizItem, ReviewLog, ReviewState


def compute_stats(session: Session, user_id, tz=None, now: datetime | None = None) -> dict:
    """Answered/accuracy/streak count every answer in review_logs, not just
    each item's latest; streak days are local dates in `tz` (settings.tz)."""
    if tz is None:
        from app.config import settings

        tz = settings.tz
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

    days = {
        _aware(at).astimezone(tz).date()
        for at in session.exec(
            select(ReviewLog.answered_at).where(ReviewLog.user_id == user_id)
        )
    }
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    cursor = today if today in days else today - timedelta(days=1)
    streak = 0
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)

    return {
        "answered": answered,
        "accuracy": round(by_verdict.get("correct", 0) / answered, 3) if answered else None,
        "streak_days": streak,
        "items_total": items_total,
        "lapses": lapses,
    }
