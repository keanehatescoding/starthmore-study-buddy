"""Job queue tests: ordering, success, retry-with-backoff, terminal failure,
stale-running reaper, and the worker's single notify path."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app import worker
from app.jobs import HANDLERS, RUNNING_TIMEOUT, enqueue, run_due
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


def _set(session, job, **fields):
    for k, v in fields.items():
        setattr(job, k, v)
    session.add(job)
    session.commit()


def test_future_job_not_claimed(session, fake_handler):
    job = enqueue(session, "fake", {"n": 1})
    _set(session, job, available_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    assert run_due(session)["completed"] == 0
    assert fake_handler == []


def test_stale_running_job_reaped_and_rerun(session, fake_handler):
    job = enqueue(session, "fake", {"n": 1})
    old = datetime.now(timezone.utc) - RUNNING_TIMEOUT - timedelta(minutes=1)
    _set(session, job, status="running", attempts=1, updated_at=old)
    out = run_due(session)
    assert out["reaped"] == 1 and out["completed"] == 1
    row = session.get(Job, job.id)
    assert row.status == "completed" and row.attempts == 2


def test_stale_running_job_out_of_attempts_fails(session, fake_handler):
    job = enqueue(session, "fake", {"n": 1}, max_attempts=1)
    old = datetime.now(timezone.utc) - RUNNING_TIMEOUT - timedelta(minutes=1)
    _set(session, job, status="running", attempts=1, updated_at=old)
    run_due(session)
    row = session.get(Job, job.id)
    assert row.status == "failed" and "timed out" in row.error
    assert fake_handler == []


def test_fresh_running_job_left_alone(session, fake_handler):
    job = enqueue(session, "fake", {"n": 1})
    _set(session, job, status="running", attempts=1)
    assert run_due(session) == {"completed": 0, "failed": 0, "retried": 0}
    assert session.get(Job, job.id).status == "running"


def test_handler_db_error_rolled_back_and_recorded(session, monkeypatch):
    def broken(s, payload):
        s.add(Job(type=None, payload={}))  # NOT NULL violation on flush
        s.flush()

    monkeypatch.setitem(HANDLERS, "broken", broken)
    job = enqueue(session, "broken", {}, max_attempts=1)
    assert run_due(session)["failed"] == 1
    row = session.get(Job, job.id)
    assert row.status == "failed" and "IntegrityError" in row.error
    assert len(session.exec(select(Job)).all()) == 1


@pytest.fixture()
def worker_engine(monkeypatch):
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(worker, "engine", engine)
    monkeypatch.setattr(worker, "ping_healthcheck", lambda: None)
    return engine


def test_worker_drains_queue_and_notifies_once(worker_engine, monkeypatch):
    order: list = []
    monkeypatch.setitem(HANDLERS, "fake", lambda s, p: order.append(p["n"]))
    monkeypatch.setitem(
        HANDLERS, "send_notifications", lambda s, p: order.append("notify")
    )
    with Session(worker_engine) as s:
        for n in range(8):  # more than one run_due batch
            enqueue(s, "fake", {"n": n})
    out = worker.run_once()
    assert out["completed"] == 9
    assert order == [*range(8), "notify"]  # syncs first, one notify pass
    worker.run_once()
    assert order.count("notify") == 2


def test_worker_skips_enqueue_when_notify_already_queued(worker_engine, monkeypatch):
    calls: list = []
    monkeypatch.setitem(HANDLERS, "send_notifications", lambda s, p: calls.append(1))
    with Session(worker_engine) as s:
        enqueue(s, "send_notifications")
    worker.run_once()
    assert calls == [1]
    with Session(worker_engine) as s:
        assert len(s.exec(select(Job)).all()) == 1
