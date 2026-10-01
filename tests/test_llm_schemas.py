"""Schemas for LLM replies: coerce what models send, reject the rest."""

import pytest
from pydantic import ValidationError

from app.llm_schemas import ChunkReply, GradeOut, QuizItemOut, QuizReply, valid_entries

MCQ = {"question": " Which? ", "question_type": "mcq", "difficulty": "recall",
       "options": ["a", " b ", 3, 4.5], "correct_answer": "2.0"}
SHORT = {"question": "Why?", "question_type": "short_answer", "difficulty": "synthesis",
         "correct_answer": "because", "grading_criteria": ["one", " ", "two "]}


def test_mcq_is_normalized():
    item = QuizItemOut.model_validate(MCQ)
    assert item.question == "Which?"
    assert item.options == ["a", "b", "3", "4.5"]
    assert item.correct_answer == "2"
    assert item.explanation is None and item.grading_criteria is None


@pytest.mark.parametrize("patch", [
    {"options": None}, {"options": ["a", "b", "c"]}, {"options": ["a", "b", "c", " "]},
    {"options": ["a", "b", "c", {"x": 1}]}, {"correct_answer": 4}, {"correct_answer": -1},
    {"correct_answer": True}, {"correct_answer": 1.5}, {"question_type": "essay"},
    {"difficulty": "hard"}, {"question": "  "},
])
def test_bad_mcq_rejected(patch):
    with pytest.raises(ValidationError):
        QuizItemOut.model_validate({**MCQ, **patch})


def test_mcq_without_options_rejected():
    with pytest.raises(ValidationError):
        QuizItemOut.model_validate({k: v for k, v in MCQ.items() if k != "options"})


def test_short_answer_joins_criteria_and_drops_options():
    item = QuizItemOut.model_validate({**SHORT, "options": ["stray"]})
    assert item.grading_criteria == "one\ntwo" and item.options is None


@pytest.mark.parametrize("patch", [{"grading_criteria": None}, {"correct_answer": ""},
                                   {"correct_answer": {"a": 1}}])
def test_ungradable_short_answer_rejected(patch):
    with pytest.raises(ValidationError):
        QuizItemOut.model_validate({**SHORT, **patch})


def test_lists_drop_only_the_bad_entries():
    reply = QuizReply.model_validate({"items": [MCQ, "junk", None, {**MCQ, "options": []}, SHORT]})
    assert [i.question_type for i in reply.items] == ["mcq", "short_answer"]
    assert QuizReply.model_validate({"items": "nope"}).items == []
    assert valid_entries(QuizItemOut, {"a": MCQ}) == []


def test_chunks():
    reply = ChunkReply.model_validate({"chunks": [
        {"title": "  T  ", "content": "  verbatim  "}, {"content": "x", "title": 7},
        {"content": 12}, {"content": "  "}, {"title": "no content"},
    ]})
    assert [c.model_dump() for c in reply.chunks] == [
        {"title": "T", "content": "  verbatim  "},  # content is never stripped
        {"title": "Untitled", "content": "x"},
    ]
    assert ChunkReply.model_validate({"chunks": [{"title": "x" * 500, "content": "c"}]}) \
        .chunks[0].title == "x" * 200


@pytest.mark.parametrize("reply, credit, feedback", [
    ({}, 0.0, "No feedback provided."),
    ({"partial_credit": "0.5", "feedback": "  ok "}, 0.5, "ok"),
    ({"partial_credit": float("nan"), "correct": "TRUE"}, 1.0, "No feedback provided."),
    ({"partial_credit": 7}, 1.0, "No feedback provided."),
    ({"partial_credit": -2, "correct": True}, 0.0, "No feedback provided."),
    ({"partial_credit": True, "correct": False}, 0.0, "No feedback provided."),
])
def test_grade(reply, credit, feedback):
    graded = GradeOut.model_validate(reply)
    assert graded.partial_credit == credit and graded.feedback == feedback
