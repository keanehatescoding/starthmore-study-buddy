"""Basic stats for the demo: accuracy, streak, queue sizes."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, func, select

from app.grade import _aware, scoped_items
from app.models import QuizItem, ReviewState


def compute_stats(session: Session, user_id) -> dict:
    states = session.exec(
        select(ReviewState).where(ReviewState.user_id == user_id)
    ).all()
    verdicts = [s.last_result for s in states if s.last_result]
    correct = sum(1 for v in verdicts if v == "correct")
    items_total = session.exec(
        scoped_items(user_id).with_only_columns(func.count(QuizItem.id))
    ).one()

    days = {
        _aware(s.answered_at).date()
        for s in states
        if s.answered_at is not None
    }
    today = datetime.now(timezone.utc).date()
    yesterday = today.fromordinal(today.toordinal() - 1)
    streak = 0
    cursor = today if today in days else (yesterday if yesterday in days else None)
    while cursor is not None and cursor in days:
        streak += 1
        cursor = cursor.fromordinal(cursor.toordinal() - 1)

    return {
        "answered": len(verdicts),
        "accuracy": round(correct / len(verdicts), 3) if verdicts else None,
        "streak_days": streak,
        "items_total": items_total,
        "lapses": sum(s.lapses for s in states),
    }
