"""Test database engines: in-memory SQLite by default, Postgres when
TEST_DATABASE_URL is set (CI does), so dialect-specific SQL (upserts,
FOR UPDATE SKIP LOCKED, partial indexes) runs against the production engine.

On Postgres every test starts from an empty `public` schema (conftest's
autouse fixture), so point TEST_DATABASE_URL at a scratch database only.
"""

import os

from sqlalchemy import create_engine, text

from app.dburl import normalize_database_url

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
_engines = []


def make_engine(sqlite_url: str = "sqlite://", **sqlite_kw):
    """An engine for one test; the SQLite arguments are ignored on Postgres."""
    if not TEST_DATABASE_URL:
        return create_engine(sqlite_url, **sqlite_kw)
    engine = create_engine(normalize_database_url(TEST_DATABASE_URL))
    _engines.append(engine)
    return engine


def reset_postgres() -> None:
    """Drop everything the previous test left behind."""
    while _engines:
        _engines.pop().dispose()
    admin = create_engine(normalize_database_url(TEST_DATABASE_URL),
                          isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        # a session a test never closed would block the DROP
        c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                       "WHERE datname = current_database() AND pid <> pg_backend_pid()"))
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    admin.dispose()
