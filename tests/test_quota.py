"""Quota tests: 429s stop the run early instead of burning quota per chunk."""

import io
import json
import urllib.error

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import app.llm as llm_mod
from app.llm import LLMClient, LLMError, QuotaExhaustedError
from app.models import Chunk, Resource
from app.pipeline import run_quiz


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


def _resp(payload: dict):
    body = json.dumps(
        {"choices": [{"message": {"content": json.dumps(payload)}}]}
    ).encode()

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    return FakeResp()


def _http_error(code: int):
    return urllib.error.HTTPError(
        "https://x", code, "err", {"Retry-After": "0"}, io.BytesIO()
    )


def _client(monkeypatch, script):
    calls = []
    it = iter(script)

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        outcome = next(it)
        if isinstance(outcome, Exception):
            raise outcome
        return _resp(outcome)

    monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake_urlopen)
    return LLMClient("https://x/v1", "key", "m"), calls


def test_three_429s_raise_quota_and_stop_at_three_calls(monkeypatch):
    client, calls = _client(
        monkeypatch, [_http_error(429)] * 5
    )
    with pytest.raises(QuotaExhaustedError):
        client.complete_json("s", "u")
    assert len(calls) == 3  # not 5 — quota failures fail fast


def test_transient_500_then_success(monkeypatch):
    client, calls = _client(
        monkeypatch, [_http_error(500), {"chunks": []}]
    )
    assert client.complete_json("s", "u") == {"chunks": []}
    assert len(calls) == 2


def test_two_429s_then_success_recovers(monkeypatch):
    client, calls = _client(
        monkeypatch, [_http_error(429), _http_error(429), {"ok": True}]
    )
    assert client.complete_json("s", "u") == {"ok": True}
    assert len(calls) == 3


def test_non_retryable_raises_immediately(monkeypatch):
    client, calls = _client(monkeypatch, [_http_error(400)])
    with pytest.raises(LLMError, match="chat completion failed:"):
        client.complete_json("s", "u")
    assert len(calls) == 1


def _bare_chunks(session, n=2):
    from app.models import Course, Topic
    course = Course(source="moodle", source_id="c1", name="C")
    session.add(course)
    session.commit()
    session.refresh(course)
    topic = Topic(course_id=course.id, source_id="t1", title="T")
    session.add(topic)
    session.commit()
    session.refresh(topic)
    for i in range(n):
        res = Resource(topic_id=topic.id, source="moodle", source_id=f"r{i}",
                       type="page_text", title=f"R{i}", status="extracted",
                       extracted_text="t")
        session.add(res)
        session.commit()
        session.refresh(res)
        session.add(Chunk(resource_id=res.id, title=f"C{i}", content="t",
                          order=0))
    session.commit()


def test_run_quiz_aborts_on_quota():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        _bare_chunks(s, 2)

        class DeadLLM:
            calls = 0

            def complete_json(self, *a):
                type(self).calls += 1
                raise QuotaExhaustedError("gone")

        counts = run_quiz(s, DeadLLM(), pace=0)
        assert counts.get("quota_exhausted") is True
        assert counts["items"] == 0 and DeadLLM.calls == 1  # stopped, not ground through
