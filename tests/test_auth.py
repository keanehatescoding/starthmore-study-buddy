"""Auth tests: login gating, cross-user isolation, sign-in claiming."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.auth as auth_mod
from app.config import settings
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


def _memory_session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_sign_in_upserts_and_claims(monkeypatch):
    monkeypatch.setattr(settings, "moodle_token_owner", "")
    with _memory_session() as s:
        s.add(Course(source="moodle", source_id="c9", name="Orphan"))
        s.commit()
        user = auth_mod.sign_in(s, "new@x.edu", refresh_token="rt-1")
        assert auth_mod.refresh_token_for(user) == "rt-1"
        orphan = s.exec(select(Course)).one()
        assert orphan.user_id == user.id
        # second sign-in refreshes token, keeps id
        again = auth_mod.sign_in(s, "new@x.edu", refresh_token="rt-2")
        assert again.id == user.id and auth_mod.refresh_token_for(again) == "rt-2"


def test_refresh_token_encrypted_at_rest():
    with _memory_session() as s:
        user = auth_mod.sign_in(s, "a@x.edu", refresh_token="rt-secret")
        assert user.google_refresh_token.startswith("v1:")
        assert "rt-secret" not in user.google_refresh_token
        # plaintext left over from before encryption is not trusted
        assert auth_mod.refresh_token_for(User(email="b@x.edu", google_refresh_token="rt")) is None


def test_second_user_does_not_steal_unowned_courses(monkeypatch):
    monkeypatch.setattr(settings, "moodle_token_owner", "")
    with _memory_session() as s:
        first = auth_mod.sign_in(s, "a@x.edu")
        s.add(Course(source="moodle", source_id="late", name="Synced later"))
        s.commit()
        auth_mod.sign_in(s, "b@x.edu")  # new, but not the first account
        auth_mod.sign_in(s, "a@x.edu")  # returning, not new
        assert s.exec(select(Course)).one().user_id is None
        assert first.id is not None


def test_only_token_owner_claims_unowned_courses(monkeypatch):
    monkeypatch.setattr(settings, "moodle_token_owner", "Owner@x.edu")
    with _memory_session() as s:
        s.add(Course(source="moodle", source_id="c9", name="Orphan"))
        s.commit()
        auth_mod.sign_in(s, "someone@x.edu")  # first account, but not the owner
        assert s.exec(select(Course)).one().user_id is None
        owner = auth_mod.sign_in(s, "owner@x.edu")
        assert s.exec(select(Course)).one().user_id == owner.id


def test_email_is_case_insensitive():
    with _memory_session() as s:
        a = auth_mod.sign_in(s, " Test@X.edu ")
        b = auth_mod.sign_in(s, "test@x.edu")
        assert a.id == b.id and b.email == "test@x.edu"
        assert len(s.exec(select(User)).all()) == 1


def test_legacy_mixed_case_row_is_matched_and_normalized():
    with _memory_session() as s:
        legacy = User(email="Legacy@X.edu")
        s.add(legacy)
        s.commit()
        user = auth_mod.sign_in(s, "legacy@x.edu")
        assert user.id == legacy.id and user.email == "legacy@x.edu"


def _userinfo(monkeypatch, info: dict):
    import io
    import json

    monkeypatch.setattr(
        auth_mod.urllib.request, "urlopen",
        lambda req, timeout: io.BytesIO(json.dumps(info).encode()),
    )


def test_fetch_email_requires_verified(monkeypatch):
    _userinfo(monkeypatch, {"email": "a@x.edu", "email_verified": False})
    with pytest.raises(auth_mod.AuthError):
        auth_mod.fetch_email("tok")
    _userinfo(monkeypatch, {"email": "a@x.edu"})
    with pytest.raises(auth_mod.AuthError):
        auth_mod.fetch_email("tok")


def test_fetch_email_normalizes(monkeypatch):
    _userinfo(monkeypatch, {"email": "Owner@X.edu", "email_verified": True})
    assert auth_mod.fetch_email("tok") == "owner@x.edu"


def test_default_secret_key_rejected_in_prod(monkeypatch):
    from pydantic import ValidationError

    from app.config import INSECURE_SECRET_KEY, Settings

    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    Settings(_env_file=None, secret_key=INSECURE_SECRET_KEY)  # dev is fine
    with pytest.raises(ValidationError):
        Settings(_env_file=None, secret_key=INSECURE_SECRET_KEY, session_secure_cookie=True)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, secret_key=INSECURE_SECRET_KEY)
    Settings(_env_file=None, secret_key="a-real-random-value")


def test_logout_is_csrf_checked_post(testapp):
    import re

    client = testapp["client"]
    assert client.get("/logout").status_code == 405
    assert client.post("/logout", data={"csrf_token": "bogus"}).status_code == 403
    page = client.get("/")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    r = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


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


def _callback_client(email: str, monkeypatch):
    """TestClient with a signed session holding oauth_state + mocked Google."""
    import json
    from base64 import b64encode

    import itsdangerous
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine

    from app.config import settings
    from app.db import get_session

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    def override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr(
        auth_mod, "exchange_code", lambda *a: {"access_token": "tok"}
    )
    monkeypatch.setattr(auth_mod, "fetch_email", lambda tok: email)
    client = TestClient(app, follow_redirects=False)
    signer = itsdangerous.TimestampSigner(str(settings.secret_key))
    raw = b64encode(json.dumps({"oauth_state": "s1"}).encode()).decode()
    client.cookies.set("session", signer.sign(raw).decode())
    return client


def test_allowlist_blocks_stranger(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "allowed_emails", "owner@x.edu")
    client = _callback_client("stranger@x.com", monkeypatch)
    try:
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_allowlist_permits_owner(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "allowed_emails", "owner@x.edu")
    client = _callback_client("owner@x.edu", monkeypatch)
    try:
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        assert r.status_code == 303 and r.headers["location"] == "/"
    finally:
        app.dependency_overrides.clear()


def test_empty_allowlist_permits_anyone(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "allowed_emails", "")
    client = _callback_client("anyone@x.com", monkeypatch)
    try:
        r = client.get("/auth/callback", params={"code": "c", "state": "s1"})
        assert r.status_code == 303
    finally:
        app.dependency_overrides.clear()
