"""Ops tests: DB-aware /health and worker healthcheck ping."""

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.db import get_session
from app.main import app
from app.worker import ping_healthcheck


def _client(engine):
    def override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _engine(with_tables: bool):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    if with_tables:
        SQLModel.metadata.create_all(engine)
    return engine


def test_health_ok_when_db_reachable():
    for client in _client(_engine(True)):
        r = client.get("/health")
        assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_503_when_db_unreachable():
    for client in _client(_engine(False)):  # tables missing -> query fails
        r = client.get("/health")
        assert r.status_code == 503
        assert r.json() == {"status": "degraded", "db": "unreachable"}


def test_ping_disabled_by_default(monkeypatch):
    called = []
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: called.append(1)
    )
    monkeypatch.setattr(settings, "healthcheck_ping_url", "")
    ping_healthcheck()
    assert called == []


def test_ping_called_on_success(monkeypatch):
    called = []

    class FakeResp:
        def read(self):
            return b"ok"

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: (called.append(1), FakeResp())[1]
    )
    monkeypatch.setattr(settings, "healthcheck_ping_url", "https://x/ping")
    ping_healthcheck()
    assert called == [1]


def test_ping_failure_never_fails_run(monkeypatch):
    def boom(*a, **k):
        raise OSError("monitor down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setattr(settings, "healthcheck_ping_url", "https://x/ping")
    ping_healthcheck()  # must not raise
