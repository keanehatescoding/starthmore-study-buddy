"""Phase 3 tests: generation validation + idempotency (FakeLLM)."""

import uuid

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Chunk, QuizItem, Resource, Topic, Course
from app.quiz import chunk_needs_quiz, generate_for_chunk


class FakeLLM:
    def __init__(self, items):
        self.items = items
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        return {"items": self.items}


GOOD = [
    {"question": "What is a tree?", "question_type": "short_answer",
     "correct_answer": "hierarchical data structure",
     "grading_criteria": "mentions hierarchy; mentions nodes",
     "explanation": "Because slides say so.", "difficulty": "recall"},
    {"question": "Which is a tree?", "question_type": "mcq",
     "options": ["array", "tree", "queue", "stack"], "correct_answer": 1,
     "explanation": "Trees branch.", "difficulty": "application"},
    {"question": "Bad mcq", "question_type": "mcq",
     "options": ["only-two"], "correct_answer": 0,
     "explanation": "x", "difficulty": "recall"},
    {"question": "", "question_type": "short_answer",
     "correct_answer": "x", "grading_criteria": "y",
     "explanation": "z", "difficulty": "recall"},
]


@pytest.fixture()
def setup():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        course = Course(source="moodle", source_id="c1", name="C")
        s.add(course)
        s.commit()
        topic = Topic(course_id=course.id, source_id="t1", title="T")
        s.add(topic)
        s.commit()
        res = Resource(topic_id=topic.id, source="moodle", source_id="r1",
                       type="file", title="R", status="extracted",
                       extracted_text="trees")
        s.add(res)
        s.commit()
        chunk = Chunk(resource_id=res.id, title="Ch", content="trees", order=0)
        s.add(chunk)
        s.commit()
        s.refresh(chunk)
        yield s, chunk


def test_generates_and_filters_invalid(setup):
    s, chunk = setup
    llm = FakeLLM(GOOD)
    items = generate_for_chunk(s, chunk, llm)
    assert len(items) == 2  # 2 invalid dropped
    assert llm.calls == 1
    mcq = s.exec(select(QuizItem).where(QuizItem.question_type == "mcq")).one()
    assert mcq.options == ["array", "tree", "queue", "stack"]
    assert mcq.correct_answer == "1"  # index stored as string
    short = s.exec(select(QuizItem).where(QuizItem.question_type == "short_answer")).one()
    assert "hierarchy" in short.grading_criteria
    assert not chunk_needs_quiz(s, chunk.id)


def test_retry_is_idempotent(setup):
    s, chunk = setup
    llm = FakeLLM(GOOD)
    first = generate_for_chunk(s, chunk, llm)
    second = generate_for_chunk(s, chunk, llm)  # e.g. after a timeout
    assert llm.calls == 1  # no second LLM call
    assert {i.id for i in second} == {i.id for i in first}
    assert len(s.exec(select(QuizItem)).all()) == 2


def test_new_attempt_generates_more(setup):
    s, chunk = setup
    llm = FakeLLM(GOOD)
    generate_for_chunk(s, chunk, llm, attempt=1)
    assert chunk_needs_quiz(s, chunk.id, attempt=2)
    generate_for_chunk(s, chunk, llm, attempt=2)
    assert s.exec(select(QuizItem)).all().__len__() == 4
    assert uuid.uuid4()  # keys are unique per attempt
