"""Pydantic schemas for LLM replies (chunks, quiz items, grades).

Model output is untrusted and loosely typed: JSON numbers arrive as
strings, booleans as "true", key points as lists. Each schema coerces the
forms models actually send and rejects the rest, so callers get typed
objects or a ValidationError, never a half-checked dict.

List replies are validated entry by entry (valid_entries): one malformed
chunk or quiz item is dropped, not the whole reply.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    StrictStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

M = TypeVar("M", bound=BaseModel)


def valid_entries(model: type[M], raw) -> list[M]:
    """The entries of a reply's list that validate as `model`; anything
    else (a non-list, a malformed entry) is dropped."""
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        try:
            out.append(model.model_validate(entry))
        except ValidationError:
            continue
    return out


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


def _flag(value) -> bool | None:
    """A JSON boolean, tolerating "true"/"false" strings (bool("false") is True)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _credit(value) -> float | None:
    """A number, or None for missing, garbage or NaN."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


Text = Annotated[str, BeforeValidator(_text)]
OptionalText = Annotated[str | None, BeforeValidator(lambda v: _text(v) or None)]


class ChunkOut(BaseModel):
    title: Annotated[str, BeforeValidator(
        lambda v: v.strip()[:200] if isinstance(v, str) and v.strip() else "Untitled"
    )] = Field("Untitled", validate_default=True)
    content: StrictStr  # copied verbatim, so never coerced or stripped

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("blank chunk content")
        return v


class ChunkReply(BaseModel):
    chunks: list[ChunkOut]

    @field_validator("chunks", mode="before")
    @classmethod
    def _drop_bad(cls, v):
        return valid_entries(ChunkOut, v)


class QuizItemOut(BaseModel):
    """One generated item, its fields ready to store on a QuizItem. Field
    order matters: options and correct_answer read question_type."""

    question: Annotated[Text, Field(min_length=1)]
    question_type: Literal["mcq", "short_answer"]
    difficulty: Literal["recall", "application", "synthesis"]
    explanation: OptionalText = None
    grading_criteria: OptionalText = None
    options: list[str] | None = Field(None, validate_default=True)
    correct_answer: str

    @field_validator("options", mode="before")
    @classmethod
    def _four_options(cls, v, info: ValidationInfo):
        if info.data.get("question_type") != "mcq":
            return None
        if (not isinstance(v, list) or len(v) != 4
                or not all(isinstance(o, (str, int, float)) and str(o).strip() for o in v)):
            raise ValueError("an MCQ needs exactly 4 non-blank options")
        return [str(o).strip() for o in v]

    @field_validator("correct_answer", mode="before")
    @classmethod
    def _answer(cls, v, info: ValidationInfo):
        if info.data.get("question_type") != "mcq":
            return _text(v)
        idx = _mcq_answer(v)
        if idx is None or not 0 <= idx < 4:
            raise ValueError("an MCQ answer must be an option index 0-3")
        return str(idx)  # stored as "2", the form the grader compares a submitted index to

    @model_validator(mode="after")
    def _short_answer_is_gradable(self):
        if self.question_type == "short_answer" and not (
            self.grading_criteria and self.correct_answer
        ):
            raise ValueError("a short answer needs a reference answer and grading criteria")
        return self


class QuizReply(BaseModel):
    items: list[QuizItemOut]

    @field_validator("items", mode="before")
    @classmethod
    def _drop_bad(cls, v):
        return valid_entries(QuizItemOut, v)


class GradeOut(BaseModel):
    """A short-answer grade. Never fails on a dict: a missing or garbage
    partial_credit falls back to the grader's correct flag."""

    correct: Annotated[bool | None, BeforeValidator(_flag)] = None
    partial_credit: Annotated[float | None, BeforeValidator(_credit)] = None
    feedback: Annotated[str, BeforeValidator(
        lambda v: str(v or "").strip() or "No feedback provided."
    )] = Field("No feedback provided.", validate_default=True)

    @model_validator(mode="after")
    def _score(self):
        if self.partial_credit is None:
            # fall back to the grader's verdict instead of scoring it a lapse
            self.partial_credit = 1.0 if self.correct else 0.0
        self.partial_credit = min(1.0, max(0.0, self.partial_credit))
        return self
