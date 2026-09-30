"""SM-2 helpers. Contract locked in Phase 0 so grading (Phase 4) and
scheduling stay in sync: grading returns partial_credit 0.0-1.0,
SM-2 expects quality 0-5."""

import math
from datetime import datetime, timedelta, timezone

# One pair of thresholds for both the verdict shown and the schedule: credit at
# or above PASS_CREDIT is a pass (SM-2 quality >= 3), below it a lapse.
PASS_CREDIT = 0.6
PARTIAL_CREDIT = 0.3


def partial_credit_to_quality(partial_credit: float) -> int:
    """Map 0.0-1.0 grading signal to SM-2 quality 0-5. Clamps input.

    Floors each 0.2 band so quality >= 3 exactly when credit >= PASS_CREDIT
    (round() is banker's rounding: 0.5 lapsed, 0.55 passed but showed "partial").
    """
    clamped = min(1.0, max(0.0, partial_credit))
    return min(5, math.floor(round(clamped * 5, 9)))


def verdict(partial_credit: float) -> str:
    if partial_credit >= PASS_CREDIT:
        return "correct"
    if partial_credit >= PARTIAL_CREDIT:
        return "partial"
    return "incorrect"


def initial_ease_factor(difficulty: str) -> float:
    """Recall starts tighter, synthesis more generous (per plan)."""
    return {"recall": 2.3, "application": 2.5, "synthesis": 2.7}.get(difficulty, 2.5)


def next_interval_days(
    quality: int, repetitions: int, ease_factor: float, interval_days: int
) -> tuple[int, int, float]:
    """One SM-2 step. Returns (new_interval_days, new_repetitions, new_ease)."""
    ease = max(1.3, ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))
    if quality < 3:
        return 1, 0, ease
    if repetitions == 0:
        interval = 1
    elif repetitions == 1:
        interval = 3
    else:
        interval = max(1, round(interval_days * ease))
    return interval, repetitions + 1, ease


def next_review_date(interval_days: int, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now + timedelta(days=interval_days)
