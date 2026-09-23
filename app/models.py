"""SQLModel entities. Mirrors the project-plan schema (Phase 0)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Column, DateTime, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(unique=True, index=True)
    google_refresh_token: Optional[str] = Field(default=None)
    moodle_token: Optional[str] = Field(default=None)
    created_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class Course(SQLModel, table=True):
    __tablename__ = "courses"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_courses_source"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source: str = Field(index=True)  # "moodle" | "classroom"
    source_id: str = Field(index=True)
    name: str
    code: Optional[str] = Field(default=None)


class Topic(SQLModel, table=True):
    __tablename__ = "topics"
    __table_args__ = (UniqueConstraint("course_id", "source_id", name="uq_topics_course_source"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    course_id: uuid.UUID = Field(foreign_key="courses.id", index=True)
    source_id: str
    title: str
    order: int = Field(default=0)


class Resource(SQLModel, table=True):
    __tablename__ = "resources"
    __table_args__ = (UniqueConstraint("topic_id", "source_id", name="uq_resources_topic_source"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    topic_id: uuid.UUID = Field(foreign_key="topics.id", index=True)
    source: str  # "moodle" | "classroom"
    source_id: str = Field(index=True)
    type: str  # "file" | "link" | "page_text" | "video"
    title: str
    raw_url: Optional[str] = Field(default=None)
    extracted_text: Optional[str] = Field(default=None, sa_column=Column(Text))
    content_hash: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="pending", index=True)  # pending|extracted|failed|skipped
    error: Optional[str] = Field(default=None)
    mime_type: Optional[str] = Field(default=None)


class Assignment(SQLModel, table=True):
    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("course_id", "source_id", name="uq_assignments_course_source"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    course_id: uuid.UUID = Field(foreign_key="courses.id", index=True)
    topic_id: Optional[uuid.UUID] = Field(default=None, foreign_key="topics.id")
    source: str = Field(default="moodle")
    source_id: str = Field(index=True)
    title: str
    due_date: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    description: Optional[str] = Field(default=None, sa_column=Column(Text))


class Chunk(SQLModel, table=True):
    __tablename__ = "chunks"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    resource_id: uuid.UUID = Field(foreign_key="resources.id", index=True)
    title: str
    content: str = Field(sa_column=Column(Text, nullable=False))
    order: int = Field(default=0)
    start_char: Optional[int] = Field(default=None)
    end_char: Optional[int] = Field(default=None)


class QuizItem(SQLModel, table=True):
    __tablename__ = "quiz_items"
    __table_args__ = (UniqueConstraint("generation_key", name="uq_quiz_items_gen_key"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    chunk_id: uuid.UUID = Field(foreign_key="chunks.id", index=True)
    question: str = Field(sa_column=Column(Text, nullable=False))
    question_type: str  # "mcq" | "short_answer"
    options: Optional[Any] = Field(default=None, sa_column=Column(JSON))
    correct_answer: str = Field(sa_column=Column(Text, nullable=False))  # index-as-str for mcq
    grading_criteria: Optional[str] = Field(default=None, sa_column=Column(Text))
    explanation: Optional[str] = Field(default=None, sa_column=Column(Text))
    difficulty: str = Field(default="recall")  # recall|application|synthesis
    generation_key: str = Field(index=True)  # chunk_id + attempt, idempotency


class ReviewState(SQLModel, table=True):
    __tablename__ = "review_states"
    __table_args__ = (UniqueConstraint("user_id", "quiz_item_id", name="uq_review_user_item"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    quiz_item_id: uuid.UUID = Field(foreign_key="quiz_items.id", index=True)
    ease_factor: float = Field(default=2.5)
    interval_days: int = Field(default=0)
    next_review_date: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    last_result: Optional[str] = Field(default=None)
    repetitions: int = Field(default=0)
    lapses: int = Field(default=0)
    answered_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


class NotificationEvent(SQLModel, table=True):
    __tablename__ = "notification_events"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    type: str = Field(index=True)  # "new_material" | "review_due"
    payload: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    sent: bool = Field(default=False, index=True)
    sent_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )


class Job(SQLModel, table=True):
    """Postgres-backed job queue (no Redis/Celery at this scale)."""

    __tablename__ = "jobs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    type: str = Field(index=True)  # e.g. "sync_moodle" | "sync_classroom" | "send_notifications"
    payload: Any = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    status: str = Field(default="pending", index=True)  # pending|running|completed|failed
    attempts: int = Field(default=0)
    max_attempts: int = Field(default=3)
    error: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    available_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
