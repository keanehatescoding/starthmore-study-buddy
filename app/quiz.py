"""Per-chunk quiz generation (2-4 items, MCQ + short-answer mix).

Guardrails (from plan):
- Strictly grounded in the chunk: never introduce outside facts.
- Return FEWER items rather than invent filler for thin content.
- MCQ: 4 options, distractors wrong in an instructive way.
- Short-answer: grading_criteria as 2-4 key points (not a model answer).
- Difficulty tagged recall|application|synthesis (feeds scheduler).
- Idempotent: generation_key = chunk_id + attempt. A retried call after a
  timeout returns existing rows instead of duplicating them.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.llm import LLMClient
from app.models import QuizItem

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


def _valid(item: dict) -> bool:
    if not item.get("question", "").strip():
        return False
    if item.get("difficulty") not in ("recall", "application", "synthesis"):
        return False
    if item.get("question_type") == "mcq":
        opts = item.get("options") or []
        try:
            idx = int(item.get("correct_answer"))
        except (TypeError, ValueError):
            return False
        return len(opts) == 4 and 0 <= idx < 4
    if item.get("question_type") == "short_answer":
        return bool(item.get("grading_criteria", "").strip()) and bool(
            str(item.get("correct_answer", "")).strip()
        )
    return False


def generate_for_chunk(
    session: Session, chunk, llm: LLMClient, attempt: int = 1
) -> list[QuizItem]:
    """Generate quiz items for one chunk. Idempotent per (chunk, attempt)."""
    key = f"{chunk.id}:{attempt}"
    existing = session.exec(
        select(QuizItem).where(QuizItem.generation_key.like(f"{key}%"))
    ).all()
    if existing:
        return list(existing)
    data = llm.complete_json(
        SYSTEM, f"Write quiz questions for this study material:\n\n{chunk.content}"
    )
    created = []
    for i, item in enumerate(data.get("items", [])[:4]):
        if not _valid(item):
            continue
        qtype = item["question_type"]
        row = QuizItem(
            chunk_id=chunk.id,
            question=item["question"].strip(),
            question_type=qtype,
            options=item.get("options") if qtype == "mcq" else None,
            correct_answer=str(item["correct_answer"]).strip(),
            grading_criteria=item.get("grading_criteria"),
            explanation=item.get("explanation"),
            difficulty=item["difficulty"],
            generation_key=key if len(data.get("items", [])) <= 1 else f"{key}:{i}",
        )
        session.add(row)
        created.append(row)
    session.commit()
    return created


def chunk_needs_quiz(session: Session, chunk_id, attempt: int = 1) -> bool:
    return not session.exec(
        select(QuizItem).where(
            QuizItem.chunk_id == chunk_id,
            QuizItem.generation_key.like(f"{chunk_id}:{attempt}%"),
        )
    ).first()
