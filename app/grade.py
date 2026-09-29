"""Grading + spaced-repetition scheduling (Phase 4).

- MCQ: instant index-match, no API call.
- Short-answer: one fast-LLM call, lenient on phrasing, scored against the
  item's grading_criteria key points -> {correct, partial_credit, feedback}.
- partial_credit (0.0-1.0) feeds SM-2 through the locked Phase-0 mapping
  (partial_credit_to_quality), then ReviewState advances via srs.next_interval_days.
- ReviewState rows are created lazily on first answer; items without one
  count as due immediately (equivalent to next_review_date = now at creation).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlmodel import Session, func, select

from app.llm import LLMClient
from app.models import Chunk, Course, QuizItem, Resource, ReviewState, Topic
from app.srs import initial_ease_factor, next_interval_days, partial_credit_to_quality

GRADE_SYSTEM = """You grade a student's short answer leniently on phrasing.
You are given the question, a reference answer, and the key points a correct
answer must hit (grading criteria). Award partial credit when some key points
are present. Ignore grammar/spelling unless it changes meaning.
Return JSON: {"correct": true|false, "partial_credit": 0.0-1.0, "feedback": "1-2 sentences, kind, specific"}"""


def _aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo — assume UTC for naive datetimes."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def grade_short_answer(llm: LLMClient, item: QuizItem, answer: str) -> dict:
    data = llm.complete_json(
        GRADE_SYSTEM,
        f"Question: {item.question}\nReference answer: {item.correct_answer}\n"
        f"Key points: {item.grading_criteria}\nStudent answer: {answer}",
        temperature=0.0,
    )
    try:
        partial = float(data.get("partial_credit", 0.0))
    except (TypeError, ValueError):
        partial = 0.0
    partial = min(1.0, max(0.0, partial))
    feedback = str(data.get("feedback") or "").strip() or "No feedback provided."
    return {
        "correct": bool(data.get("correct", False)) or partial >= 0.6,
        "partial_credit": partial,
        "feedback": feedback,
    }


class InvalidAnswer(ValueError):
    """The submitted answer can't be graded against this item (e.g. bad MCQ index)."""


def _mcq_index(value: str, n_options: int) -> int | None:
    try:
        idx = int(str(value).strip())
    except ValueError:
        return None
    return idx if 0 <= idx < n_options else None


def grade_mcq(item: QuizItem, answer: str) -> float:
    n = len(item.options or [])
    chosen = _mcq_index(answer, n)
    if chosen is None:
        raise InvalidAnswer(f"answer must be an option index 0-{n - 1}")
    return 1.0 if chosen == _mcq_index(item.correct_answer, n) else 0.0


def _verdict(partial: float) -> str:
    if partial >= 0.6:
        return "correct"
    if partial >= 0.3:
        return "partial"
    return "incorrect"


def submit_answer(
    session: Session, user_id, quiz_item_id, answer: str, llm: LLMClient | None = None
) -> dict:
    """Grade an answer and advance the SM-2 schedule. Returns the outcome."""
    item = session.get(QuizItem, quiz_item_id)
    if item is None:
        raise ValueError(f"quiz item {quiz_item_id} not found")

    if item.question_type == "mcq":
        partial = grade_mcq(item, answer)
        feedback = item.explanation or ""
    else:
        if llm is None:
            raise ValueError("short-answer grading needs an LLM client")
        graded = grade_short_answer(llm, item, answer)
        partial, feedback = graded["partial_credit"], graded["feedback"]

    quality = partial_credit_to_quality(partial)
    state = session.exec(
        select(ReviewState).where(
            ReviewState.user_id == user_id, ReviewState.quiz_item_id == item.id
        )
    ).first()
    now = datetime.now(timezone.utc)
    if state is None:
        state = ReviewState(
            user_id=user_id, quiz_item_id=item.id,
            ease_factor=initial_ease_factor(item.difficulty),
            interval_days=0, next_review_date=now,
        )
    interval, reps, ease = next_interval_days(
        quality, state.repetitions, state.ease_factor, state.interval_days
    )
    state.interval_days = interval
    state.repetitions = reps
    state.ease_factor = ease
    state.last_result = _verdict(partial)
    if quality < 3:
        state.lapses += 1
    state.next_review_date = now + timedelta(days=interval)
    state.answered_at = now
    session.add(state)
    session.commit()
    session.refresh(state)
    return {
        "correct": partial >= 0.6,
        "partial_credit": partial,
        "quality": quality,
        "feedback": feedback,
        "verdict": state.last_result,
        "interval_days": state.interval_days,
        "repetitions": state.repetitions,
        "next_review_date": _aware(state.next_review_date),
    }


def scoped_items(user_id):
    """QuizItems in the user's courses. Unclaimed pre-auth rows belong to
    nobody until their owner signs in or syncs (see auth.sign_in, sync)."""
    return (
        select(QuizItem)
        .join(Chunk, Chunk.id == QuizItem.chunk_id)
        .join(Resource, Resource.id == Chunk.resource_id)
        .join(Topic, Topic.id == Resource.topic_id)
        .join(Course, Course.id == Topic.course_id)
        .where(Course.user_id == user_id)
    )


def user_owns_item(session: Session, user_id, item_id) -> bool:
    return session.exec(
        scoped_items(user_id).where(QuizItem.id == item_id).with_only_columns(QuizItem.id)
    ).first() is not None


def _due(user_id, now: datetime):
    """Scoped items with no ReviewState for this user, or one that's come due."""
    return scoped_items(user_id).outerjoin(
        ReviewState,
        and_(ReviewState.quiz_item_id == QuizItem.id, ReviewState.user_id == user_id),
    ).where(or_(ReviewState.id.is_(None), ReviewState.next_review_date <= now))


def due_items(session: Session, user_id, limit: int = 20) -> list[QuizItem]:
    """Review queue: new items (no ReviewState) first, then most-overdue."""
    now = datetime.now(timezone.utc)
    return list(session.exec(
        _due(user_id, now)
        .order_by(
            ReviewState.id.is_not(None), ReviewState.next_review_date,
            Topic.order, Chunk.order, QuizItem.generation_key,
        )
        .limit(limit)
    ).all())


def due_count(session: Session, user_id) -> int:
    now = datetime.now(timezone.utc)
    return session.exec(
        _due(user_id, now).with_only_columns(func.count(QuizItem.id))
    ).one()
