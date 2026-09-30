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

    def complete_json(self, system, user, **kw):
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


def test_attempt_1_does_not_reuse_attempt_10(setup):
    # Regression: LIKE '{chunk}:1%' used to match '{chunk}:10...' rows.
    s, chunk = setup
    llm = FakeLLM(GOOD)
    tenth = generate_for_chunk(s, chunk, llm, attempt=10)
    assert all(i.generation_key.startswith(f"{chunk.id}:10") for i in tenth)
    assert chunk_needs_quiz(s, chunk.id, attempt=1)
    first = generate_for_chunk(s, chunk, llm, attempt=1)
    assert llm.calls == 2
    assert not {i.id for i in first} & {i.id for i in tenth}
    assert sorted(i.generation_key for i in first) == [
        f"{chunk.id}:1:0", f"{chunk.id}:1:1"
    ]


def test_keys_index_valid_items_not_raw_positions(setup):
    # Invalid items first: raw-index keys would be :1/:3, valid-index keys :0/:1.
    s, chunk = setup
    llm = FakeLLM([GOOD[2], GOOD[0], GOOD[3], GOOD[1]])
    items = generate_for_chunk(s, chunk, llm)
    assert [i.generation_key for i in items] == [
        f"{chunk.id}:1:0", f"{chunk.id}:1:1"
    ]


def test_single_valid_item_gets_bare_key(setup):
    # One valid item among invalid ones: counting raw items would add ':i'.
    s, chunk = setup
    llm = FakeLLM([GOOD[2], GOOD[0], GOOD[3]])
    items = generate_for_chunk(s, chunk, llm)
    assert [i.generation_key for i in items] == [f"{chunk.id}:1"]
    assert not chunk_needs_quiz(s, chunk.id)


@pytest.mark.parametrize("raw, stored", [(2, "2"), (2.0, "2"), ("2.0", "2"), (" 2 ", "2"),
                                         (True, None), (1.5, None), ("two", None)])
def test_mcq_index_normalized(setup, raw, stored):
    s, chunk = setup
    item = dict(GOOD[1], correct_answer=raw)
    generate_for_chunk(s, chunk, FakeLLM([item]))
    rows = s.exec(select(QuizItem)).all()
    assert [r.correct_answer for r in rows] == ([stored] if stored else [])


def test_zero_item_chunk_is_not_billed_again(setup):
    s, chunk = setup
    llm = FakeLLM([GOOD[2]])  # nothing valid
    assert generate_for_chunk(s, chunk, llm) == []
    assert not chunk_needs_quiz(s, chunk.id)
    assert generate_for_chunk(s, chunk, llm) == [] and llm.calls == 1
    assert chunk_needs_quiz(s, chunk.id, attempt=2)  # a new attempt may retry


def test_every_empty_attempt_stays_done(setup):
    # One marker per chunk would forget attempt 1 once attempt 2 ran.
    s, chunk = setup
    llm = FakeLLM([GOOD[2]])  # nothing valid
    generate_for_chunk(s, chunk, llm, attempt=1)
    generate_for_chunk(s, chunk, llm, attempt=2)
    assert llm.calls == 2
    for attempt in (1, 2, 1, 2):
        assert not chunk_needs_quiz(s, chunk.id, attempt=attempt)
        assert generate_for_chunk(s, chunk, llm, attempt=attempt) == []
    assert llm.calls == 2
    assert chunk_needs_quiz(s, chunk.id, attempt=3)


@pytest.mark.parametrize("patch", [
    {"question": None}, {"explanation": None}, {"grading_criteria": None},
    {"correct_answer": None}, {"question": ["a"]}, {"difficulty": 3},
])
def test_null_or_odd_fields_do_not_crash(setup, patch):
    s, chunk = setup
    generate_for_chunk(s, chunk, FakeLLM([dict(GOOD[0], **patch), GOOD[1]]))
    assert len(s.exec(select(QuizItem)).all()) >= 1  # the good item survives


def test_list_grading_criteria_is_joined(setup):
    s, chunk = setup
    item = dict(GOOD[0], grading_criteria=["mentions hierarchy", "mentions nodes"])
    generate_for_chunk(s, chunk, FakeLLM([item]))
    row = s.exec(select(QuizItem)).one()
    assert row.grading_criteria == "mentions hierarchy\nmentions nodes"


def test_items_not_a_list_yields_nothing(setup):
    s, chunk = setup
    assert generate_for_chunk(s, chunk, FakeLLM("nope")) == []
