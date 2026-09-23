"""Job queue tests: ordering, success, retry-with-backoff, terminal failure."""

from datetime import datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.jobs import HANDLERS, enqueue, run_due
from app.models import Job


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def fake_handler(monkeypatch):
    calls: list = []

    def fake(session, payload):
        calls.append(payload)
        if payload.get("fail"):
            raise RuntimeError("boom")
        return {"ok": True}

    monkeypatch.setitem(HANDLERS, "fake", fake)
    return calls


def test_success_completed_with_result(session, fake_handler):
    job = enqueue(session, "fake", {"n": 1})
    out = run_due(session)
    assert out == {"completed": 1, "failed": 0, "retried": 0}
    row = session.get(Job, job.id)
    assert row.status == "completed" and row.payload["result"] == {"ok": True}
    assert fake_handler == [{"n": 1}]


def test_oldest_first_and_limit(session, fake_handler):
    enqueue(session, "fake", {"n": 1})
    enqueue(session, "fake", {"n": 2})
    run_due(session, limit=1)
    assert fake_handler == [{"n": 1}]
    run_due(session)
    assert fake_handler == [{"n": 1}, {"n": 2}]


def test_failure_retries_then_completes(session, fake_handler):
    job = enqueue(session, "fake", {"fail": True}, max_attempts=3)
    out = run_due(session)
    assert out["retried"] == 1
    row = session.get(Job, job.id)
    assert row.status == "pending" and row.attempts == 1
    assert "boom" in (row.error or "") and row.available_at > datetime(2020, 1, 1)
    # make it due again with a fixed payload -> succeeds
    row.available_at = datetime(2000, 1, 1)
    row.payload = {}
    session.add(row)
    session.commit()
    out = run_due(session)
    assert out["completed"] == 1
    assert session.get(Job, job.id).status == "completed"


def test_exhausted_attempts_fail_terminally(session, fake_handler):
    job = enqueue(session, "fake", {"fail": True}, max_attempts=1)
    out = run_due(session)
    assert out == {"completed": 0, "failed": 1, "retried": 0}
    row = session.get(Job, job.id)
    assert row.status == "failed" and "boom" in (row.error or "")


def test_unknown_type_fails_with_error(session):
    job = enqueue(session, "nope", {}, max_attempts=1)
    run_due(session)
    row = session.get(Job, job.id)
    assert row.status == "failed" and "no handler" in (row.error or "")


def test_empty_queue_noop(session):
    assert run_due(session) == {"completed": 0, "failed": 0, "retried": 0}
