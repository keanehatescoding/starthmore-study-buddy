"""Shared TestClient with isolated in-memory SQLite per test.

Authenticated: current_user is overridden to a fixture user, so web tests
exercise the owned-data paths. Auth flow itself is tested in test_auth.py.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.main import app, current_user
from app.models import User


@pytest.fixture()
def testapp():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="test@x.edu")
        s.add(user)
        s.commit()
        s.refresh(user)
        user_id = user.id

    def override_session():
        with Session(engine) as s:
            yield s

    def override_user():
        with Session(engine) as s:
            return s.get(User, user_id)

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[current_user] = override_user
    yield {"client": TestClient(app), "Session": lambda: Session(engine),
           "user_id": user_id}
    app.dependency_overrides.clear()
