"""Data migrations that can't be checked by `alembic check` alone."""

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlmodel import SQLModel

import app.models  # noqa: F401 -- populate metadata
from app.crypto import unseal

VERSIONS = Path(__file__).parent.parent / "alembic" / "versions"


def _migration(name: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _users(engine, rows):
    with engine.begin() as c:
        for i, (email, token) in enumerate(rows):
            c.execute(
                text("INSERT INTO users (id, email, google_refresh_token, created_at) "
                     "VALUES (:i, :e, :t, :d)"),
                {"i": uuid.uuid4().hex, "e": email, "t": token, "d": f"2026-01-0{i + 1}"},
            )


def _run(engine, fn):
    with engine.begin() as c, Operations.context(MigrationContext.configure(c)):
        fn()


@pytest.fixture()
def engine():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return engine


def test_0005_lowercases_one_row_per_address(engine):
    m = _migration("0005_normalize_auth")
    # two mixed-case variants with no lowercase row used to collide
    _users(engine, [("A@x.edu", None), ("a@X.edu", None), ("Dup@x.edu", None),
                    ("dup@x.edu", None), ("Solo@X.edu", "rt")])
    _run(engine, m.upgrade)
    with engine.connect() as c:
        emails = sorted(r[0] for r in c.execute(text("SELECT email FROM users")))
        token = c.execute(text(
            "SELECT google_refresh_token FROM users WHERE email = 'solo@x.edu'"
        )).scalar_one()
    assert emails == ["Dup@x.edu", "a@X.edu", "a@x.edu", "dup@x.edu", "solo@x.edu"]
    assert unseal("google-refresh-token", token) == "rt"


def test_0005_downgrade_refuses_to_null_undecryptable_tokens(engine):
    m = _migration("0005_normalize_auth")
    _users(engine, [("a@x.edu", "v1:not-decryptable")])
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _run(engine, m.downgrade)
    with engine.connect() as c:
        stored = c.execute(text("SELECT google_refresh_token FROM users")).scalar_one()
    assert stored == "v1:not-decryptable"


def test_0005_seals_with_secret_key_from_env(engine, monkeypatch):
    from app.config import settings

    m = _migration("0005_normalize_auth")
    monkeypatch.setenv("SECRET_KEY", "migration-test-key")
    monkeypatch.setattr(settings, "secret_key", "migration-test-key")
    _users(engine, [("a@x.edu", "rt")])
    _run(engine, m.upgrade)
    with engine.connect() as c:
        token = c.execute(text("SELECT google_refresh_token FROM users")).scalar_one()
    assert unseal("google-refresh-token", token) == "rt"  # the app can read it


@pytest.mark.parametrize("line", [
    "export SECRET_KEY=dotenv-key",
    "SECRET_KEY=dotenv-key # a comment",
    "SECRET_KEY='dotenv-key'",
    "secret_key=dotenv-key",
])
def test_0005_reads_dotenv_like_the_app(line, tmp_path, monkeypatch):
    from app.config import Settings

    m = _migration("0005_normalize_auth")
    (tmp_path / ".env").write_text(line + "\n")
    monkeypatch.chdir(tmp_path)
    for k in [k for k in os.environ if k.lower() == "secret_key"]:
        monkeypatch.delenv(k)
    assert m._secret_key() == Settings().secret_key == "dotenv-key"


def test_0005_env_var_beats_dotenv(tmp_path, monkeypatch):
    m = _migration("0005_normalize_auth")
    (tmp_path / ".env").write_text("SECRET_KEY=from-file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SECRET_KEY", "from-env")
    assert m._secret_key() == "from-env"


def test_0009_backfills_review_logs_from_answered_states(engine):
    m = _migration("0009_review_log")
    with engine.begin() as c:
        c.execute(text("DROP TABLE review_logs"))
        for result, answered in (("correct", "2026-09-01 10:00:00"), (None, None)):
            c.execute(
                text("INSERT INTO review_states (id, user_id, quiz_item_id, ease_factor, "
                     "interval_days, next_review_date, last_result, repetitions, lapses, "
                     "answered_at) VALUES (:i, :u, :q, 2.5, 1, '2026-09-02', :r, 1, 0, :a)"),
                {"i": uuid.uuid4().hex, "u": uuid.uuid4().hex, "q": uuid.uuid4().hex,
                 "r": result, "a": answered},
            )
    _run(engine, m.upgrade)
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT verdict, partial_credit, answered_at FROM review_logs")).all()
    assert [tuple(r) for r in rows] == [("correct", None, "2026-09-01 10:00:00")]


def test_0010_requeues_only_drive_files_that_lacked_the_scope(engine):
    m = _migration("0010_retry_drive_files")
    rows = [  # (source, status, error) -> expected status after upgrade
        ("classroom", "failed",
         "classroom drive download needs a drive scope (v1 gap)", "pending"),
        ("classroom", "failed", "unsupported type (mime=?, file=?)", "failed"),
        ("moodle", "failed", "classroom drive download needs a drive scope (v1 gap)",
         "failed"),
    ]
    with engine.begin() as c:
        for i, (source, status, error, _) in enumerate(rows):
            c.execute(text(
                "INSERT INTO resources (id, topic_id, source, source_id, type, title, status, "
                "error) VALUES (:i, :t, :s, :sid, 'file', 'R', :st, :e)"),
                {"i": uuid.uuid4().hex, "t": uuid.uuid4().hex, "s": source,
                 "sid": str(i), "st": status, "e": error})
    _run(engine, m.upgrade)
    with engine.connect() as c:
        got = dict(c.execute(text("SELECT source_id, status FROM resources")).all())
    assert got == {str(i): row[3] for i, row in enumerate(rows)}
