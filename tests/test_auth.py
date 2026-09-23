"""Auth tests: login gating, cross-user isolation, sign-in claiming."""

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.auth as auth_mod
from app.db import get_session
from app.main import app
from app.models import Course, User


def test_unauthenticated_redirects_to_login():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def override_session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    try:
        client = TestClient(app, follow_redirects=False)
        r = client.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        r = client.get("/review")
        assert r.status_code == 303
        assert client.get("/health").status_code == 200
        assert "Sign in with Google" in client.get("/login").text
    finally:
        app.dependency_overrides.clear()


def test_login_url_requests_offline_classroom_scopes():
    url = auth_mod.login_url("cid", "https://x/cb", "state123")
    assert "access_type=offline" in url
    assert "state=state123" in url
    assert "classroom" in url and "openid" in url


def test_sign_in_upserts_and_claims():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Course(source="moodle", source_id="c9", name="Orphan"))
        s.commit()
        user = auth_mod.sign_in(s, "new@x.edu", refresh_token="rt-1")
        assert user.google_refresh_token == "rt-1"
        orphan = s.exec(select(Course)).one()
        assert orphan.user_id == user.id
        # second sign-in refreshes token, keeps id
        again = auth_mod.sign_in(s, "new@x.edu", refresh_token="rt-2")
        assert again.id == user.id and again.google_refresh_token == "rt-2"


def test_cross_user_isolation(testapp):
    client, Session = testapp["client"], testapp["Session"]
    with Session() as s:
        other = User(email="other@x.edu")
        s.add(other)
        s.commit()
        s.refresh(other)
        course = Course(user_id=other.id, source="moodle", source_id="cX",
                        name="Other course")
        s.add(course)
        s.commit()
        s.refresh(course)
        cid = str(course.id)
    assert client.get(f"/courses/{cid}").status_code == 404
    assert "Other course" not in client.get("/").text
