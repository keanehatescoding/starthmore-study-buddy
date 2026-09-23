"""Phase 0 smoke tests: schema chain + SM-2 contract. Runs on SQLite."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine

from app.models import (
    Assignment,
    Chunk,
    Course,
    Job,
    NotificationEvent,
    QuizItem,
    Resource,
    ReviewState,
    Topic,
    User,
)
from app.srs import (
    initial_ease_factor,
    next_interval_days,
    partial_credit_to_quality,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _chain(s: Session):
    user = User(email="student@strathmore.edu")
    course = Course(source="moodle", source_id="42", name="Data Structures", code="CS 301")
    s.add_all([user, course])
    s.commit()
    topic = Topic(course_id=course.id, source_id="7", title="Trees", order=1)
    s.add(topic)
    s.commit()
    resource = Resource(
        topic_id=topic.id, source="moodle", source_id="99",
        type="file", title="trees.pdf", status="extracted",
        extracted_text="A tree is...", content_hash="abc",
    )
    s.add(resource)
    s.commit()
    chunk = Chunk(resource_id=resource.id, title="Definition", content="A tree is...", order=0)
    s.add(chunk)
    s.commit()
    quiz = QuizItem(
        chunk_id=chunk.id, question="What is a tree?", question_type="short_answer",
        correct_answer="hierarchical data structure", difficulty="recall",
        generation_key=f"{chunk.id}:1",
    )
    s.add(quiz)
    s.commit()
    return user, course, topic, resource, chunk, quiz


def test_full_chain(session: Session):
    user, course, topic, resource, chunk, quiz = _chain(session)
    review = ReviewState(
        user_id=user.id, quiz_item_id=quiz.id, ease_factor=2.5,
        interval_days=0, next_review_date=datetime.now(timezone.utc),
    )
    assignment = Assignment(
        course_id=course.id, topic_id=topic.id, source="moodle",
        source_id="1001", title="Assignment 1",
    )
    event = NotificationEvent(user_id=user.id, type="new_material", payload={"count": 1})
    job = Job(type="sync_moodle", payload={"course_id": "42"})
    session.add_all([review, assignment, event, job])
    session.commit()
    assert session.get(QuizItem, quiz.id).question == "What is a tree?"
    assert session.get(ReviewState, review.id).repetitions == 0


def test_generation_key_idempotent(session: Session):
    _, _, _, _, chunk, quiz = _chain(session)
    dup = QuizItem(
        chunk_id=chunk.id, question="dup?", question_type="mcq",
        options=["a", "b", "c", "d"], correct_answer="0",
        difficulty="recall", generation_key=quiz.generation_key,
    )
    session.add(dup)
    with pytest.raises(IntegrityError):
        session.commit()


def test_quality_mapping():
    assert partial_credit_to_quality(1.0) == 5
    assert partial_credit_to_quality(0.0) == 0
    assert partial_credit_to_quality(0.5) in (2, 3)  # round(2.5) == 2 (banker's)
    assert partial_credit_to_quality(0.8) == 4
    assert partial_credit_to_quality(2.0) == 5  # clamped
    assert partial_credit_to_quality(-1.0) == 0  # clamped


def test_sm2_steps():
    interval, reps, _ = next_interval_days(5, 0, 2.5, 0)
    assert (interval, reps) == (1, 1)
    interval, reps, _ = next_interval_days(5, 1, 2.5, 1)
    assert (interval, reps) == (3, 2)
    interval, reps, _ = next_interval_days(2, 3, 2.5, 7)  # fail resets
    assert (interval, reps) == (1, 0)
    assert initial_ease_factor("recall") < initial_ease_factor("synthesis")
    assert uuid.uuid4()  # sanity: uuid PKs usable
