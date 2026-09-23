"""Phase 4 tests: MCQ grading, short-answer flow, SM-2 progression, due queue."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.grade import due_items, submit_answer
from app.models import Chunk, Course, QuizItem, Resource, ReviewState, Topic, User


class FakeLLM:
    def __init__(self, partial=0.8):
        self.partial = partial

    def complete_json(self, system, user, temperature=0.0):
        return {"correct": self.partial >= 0.6, "partial_credit": self.partial,
                "feedback": "Good effort."}


@pytest.fixture()
def setup():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="s@x.edu")
        s.add(user)
        course = Course(source="moodle", source_id="c1", name="C")
        s.add(course)
        s.commit()
        topic = Topic(course_id=course.id, source_id="t1", title="T")
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source="moodle", source_id="r1",
                       type="file", title="R", status="extracted", extracted_text="t")
        s.add(res)
        s.commit()
        chunk = Chunk(resource_id=res.id, title="Ch", content="t", order=0)
        s.add(chunk)
        s.commit()
        mcq = QuizItem(chunk_id=chunk.id, question="Which?", question_type="mcq",
                       options=["a", "b", "c", "d"], correct_answer="1",
                       difficulty="recall", generation_key="g1")
        short = QuizItem(chunk_id=chunk.id, question="Explain?", question_type="short_answer",
                         correct_answer="because reasons", grading_criteria="says because",
                         difficulty="application", generation_key="g2")
        s.add_all([mcq, short])
        s.commit()
        s.refresh(user)
        s.refresh(mcq)
        s.refresh(short)
        yield s, user, mcq, short


def test_mcq_instant_no_llm(setup):
    s, user, mcq, _ = setup
    assert submit_answer(s, user.id, mcq.id, "1")["correct"] is True
    assert submit_answer(s, user.id, mcq.id, "0")["correct"] is False


def test_mcq_wrong_resets_interval(setup):
    s, user, mcq, _ = setup
    r1 = submit_answer(s, user.id, mcq.id, "1")
    assert (r1["interval_days"], r1["repetitions"]) == (1, 1)
    r2 = submit_answer(s, user.id, mcq.id, "2")
    assert (r2["interval_days"], r2["repetitions"]) == (1, 0)
    state = s.exec(select(ReviewState)).one()
    assert state.lapses == 1 and state.last_result == "incorrect"


def test_short_answer_uses_criteria(setup):
    s, user, _, short = setup
    out = submit_answer(s, user.id, short.id, "because stuff", FakeLLM(0.8))
    assert out["correct"] is True and out["quality"] == 4
    assert out["feedback"] == "Good effort."
    out = submit_answer(s, user.id, short.id, "dunno", FakeLLM(0.1))
    assert out["correct"] is False and out["verdict"] == "incorrect"


def test_interval_grows_on_streak(setup):
    s, user, mcq, _ = setup
    submit_answer(s, user.id, mcq.id, "1")
    out = submit_answer(s, user.id, mcq.id, "1")
    assert (out["interval_days"], out["repetitions"]) == (3, 2)
    assert out["next_review_date"] > datetime.now(timezone.utc)


def test_due_queue_new_first_then_overdue(setup):
    s, user, mcq, short = setup
    assert [i.id for i in due_items(s, user.id)] == [mcq.id, short.id]
    submit_answer(s, user.id, mcq.id, "1")
    submit_answer(s, user.id, short.id, "x", FakeLLM(1.0))
    assert due_items(s, user.id) == []  # nothing due yet
    # force one overdue
    state = s.exec(select(ReviewState).where(ReviewState.quiz_item_id == mcq.id)).one()
    state.next_review_date = datetime.now(timezone.utc) - timedelta(hours=1)
    s.add(state)
    s.commit()
    assert [i.id for i in due_items(s, user.id)] == [mcq.id]
