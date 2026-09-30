"""Ops tests: DB-aware /health, worker healthcheck ping, DATABASE_URL driver."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.db import get_session
from app.dburl import normalize_database_url
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


def test_ping_fail_hits_fail_endpoint(monkeypatch):
    urls: list = []

    class FakeResp:
        def read(self):
            return b"ok"

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, **k: (urls.append(url), FakeResp())[1]
    )
    monkeypatch.setattr(settings, "healthcheck_ping_url", "https://x/ping/abc/")
    ping_healthcheck(fail=True)
    assert urls == ["https://x/ping/abc/fail"]


@pytest.mark.parametrize("url, expected", [
    ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
    ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("sqlite:///x.db", "sqlite:///x.db"),
])
def test_database_url_pinned_to_psycopg(url, expected, monkeypatch):
    from app.config import Settings

    assert normalize_database_url(url) == expected
    monkeypatch.setenv("DATABASE_URL", url)
    assert Settings().database_url == expected
