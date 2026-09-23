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

from sqlmodel import Session, select

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
        partial = 1.0 if answer.strip() == item.correct_answer.strip() else 0.0
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


def due_items(session: Session, user_id, limit: int = 20) -> list[QuizItem]:
    """Review queue: new items (no ReviewState) first, then most-overdue.

    Scoped to the user's courses (owned or still-unclaimed pre-auth rows).
    """
    now = datetime.now(timezone.utc)
    owned_ids = {
        c.id
        for c in session.exec(
            select(Course).where(
                (Course.user_id == user_id) | (Course.user_id.is_(None))
            )
        ).all()
    }
    states = {
        s.quiz_item_id: s
        for s in session.exec(select(ReviewState).where(ReviewState.user_id == user_id)).all()
    }

    def in_scope(item: QuizItem) -> bool:
        chunk = session.get(Chunk, item.chunk_id)
        resource = session.get(Resource, chunk.resource_id) if chunk else None
        topic = session.get(Topic, resource.topic_id) if resource else None
        course = session.get(Course, topic.course_id) if topic else None
        return course is not None and course.id in owned_ids

    items = [i for i in session.exec(select(QuizItem)).all() if in_scope(i)]
    new = [i for i in items if i.id not in states]
    overdue = sorted(
        (i for i in items if i.id in states and _aware(states[i.id].next_review_date) <= now),
        key=lambda i: _aware(states[i.id].next_review_date),
    )
    return (new + overdue)[:limit]
