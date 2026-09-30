"""Per-chunk quiz generation (2-4 items, MCQ + short-answer mix).

Guardrails (from plan):
- Strictly grounded in the chunk: never introduce outside facts.
- Return FEWER items rather than invent filler for thin content.
- MCQ: 4 options, distractors wrong in an instructive way.
- Short-answer: grading_criteria as 2-4 key points (not a model answer).
- Difficulty tagged recall|application|synthesis (feeds scheduler).
- Idempotent: generation_key = chunk_id + attempt. A retried call after a
  timeout returns existing rows instead of duplicating them, and
  a QuizAttempt row marks each attempt done even when it yielded no items.
- Model output is untrusted: a malformed item is dropped, not the chunk.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.llm import LLMClient
from app.models import QuizAttempt, QuizItem
from sqlalchemy import or_

SYSTEM = """You write quiz questions testing study material the lecturer covered.
Rules:
- Base EVERY question strictly on the provided chunk. Never introduce facts not in it.
- Return 2-4 items, mixing "mcq" and "short_answer" by what suits the content.
  Return FEWER items rather than filler for thin content (title slides, references -> 0-1).
- MCQ: exactly 4 options; distractors must be plausible and wrong in an instructive way.
- Short-answer: "grading_criteria" lists 2-4 key points an answer must hit (not a full model answer).
- Tag each item "difficulty": recall (facts), application (use the concept), synthesis (connect ideas).
- "explanation" briefly justifies the answer, grounded in the chunk.
- Return JSON: {"items": [{"question": ..., "question_type": "mcq"|"short_answer",
  "options": [...4 strings, mcq only...], "correct_answer": <option index 0-3 for mcq, reference text for short_answer>,
  "grading_criteria": "...", "explanation": "...", "difficulty": "recall"|"application"|"synthesis"}]}"""


def _mcq_answer(value) -> int | None:
    """The model's MCQ answer as an option index. JSON gives 2, 2.0 or "2";
    a bool (int(True) == 1) or a fractional index isn't an answer."""
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def _text(value) -> str:
    """A model string field, stripped; a list of strings (criteria often come
    as key points) is one per line; anything else (null, dict) is empty."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return "\n".join(v.strip() for v in value if v.strip())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _clean(item) -> dict | None:
    """The item's fields ready to store, or None if it isn't a usable item."""
    if not isinstance(item, dict):
        return None
    question = _text(item.get("question"))
    difficulty = item.get("difficulty")
    qtype = item.get("question_type")
    if not question or difficulty not in ("recall", "application", "synthesis"):
        return None
    out = {"question": question, "question_type": qtype, "difficulty": difficulty,
           "explanation": _text(item.get("explanation")) or None,
           "grading_criteria": _text(item.get("grading_criteria")) or None}
    if qtype == "mcq":
        opts = item.get("options")
        idx = _mcq_answer(item.get("correct_answer"))
        if (not isinstance(opts, list) or len(opts) != 4
                or not all(isinstance(o, (str, int, float)) and str(o).strip() for o in opts)
                or idx is None or not 0 <= idx < 4):
            return None
        # stored as "2", the form the grader compares a submitted index to
        return {**out, "options": [str(o).strip() for o in opts], "correct_answer": str(idx)}
    if qtype == "short_answer":
        answer = _text(item.get("correct_answer"))
        if not out["grading_criteria"] or not answer:
            return None
        return {**out, "options": None, "correct_answer": answer}
    return None


def _key_matches(column, key: str):
    """Match bare key or key:<suffix> — but NOT key-prefix collisions.

    Old code used ``LIKE '{key}%'`` so attempt=1 matched attempt=10.
    Requiring the ':' delimiter fixes it while staying backward compatible
    with both single-item rows (bare key) and multi-item rows (key:i).
    """
    return or_(column == key, column.like(f"{key}:%"))


def generate_for_chunk(
    session: Session, chunk, llm: LLMClient, attempt: int = 1
) -> list[QuizItem]:
    """Generate quiz items for one chunk. Idempotent per (chunk, attempt)."""
    key = f"{chunk.id}:{attempt}"
    existing = session.exec(
        select(QuizItem).where(
            QuizItem.chunk_id == chunk.id, _key_matches(QuizItem.generation_key, key)
        )
    ).all()
    if existing or session.get(QuizAttempt, (chunk.id, attempt)) is not None:
        return list(existing)
    data = llm.complete_json(
        SYSTEM, f"Write quiz questions for this study material:\n\n{chunk.content}",
        required_key="items",
    )
    items = data["items"] if isinstance(data["items"], list) else []
    valid = [c for c in (_clean(item) for item in items) if c is not None][:4]
    created = []
    single = len(valid) <= 1
    for i, item in enumerate(valid):
        row = QuizItem(chunk_id=chunk.id, **item,
                       generation_key=key if single else f"{key}:{i}")
        session.add(row)
        created.append(row)
    session.add(QuizAttempt(chunk_id=chunk.id, attempt=attempt))
    session.commit()
    return created


def chunk_needs_quiz(session: Session, chunk_id, attempt: int = 1) -> bool:
    if session.get(QuizAttempt, (chunk_id, attempt)) is not None:
        return False  # tried, even if nothing was quizzable
    key = f"{chunk_id}:{attempt}"
    return not session.exec(
        select(QuizItem).where(
            QuizItem.chunk_id == chunk_id,
            _key_matches(QuizItem.generation_key, key),
        )
    ).first()
