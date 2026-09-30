"""Google sign-in. One consent grants login (openid/email) AND Classroom sync.

Flow: /login -> Google (with state) -> /auth/callback -> exchange code for
tokens -> fetch verified email -> upsert User (storing the refresh token for
Classroom, encrypted) -> claim legacy unowned courses -> session cookie.
"""

from __future__ import annotations

import json
import secrets
import urllib.parse
import urllib.request

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app.classroom import SCOPES as CLASSROOM_SCOPES
from app.config import settings
from app.crypto import seal, unseal
from app.drive import SCOPE as DRIVE_SCOPE
from app.models import Course, User

# drive.readonly: Classroom materials are mostly Drive files (app.drive)
LOGIN_SCOPES = ["openid", "email", "profile", *CLASSROOM_SCOPES, DRIVE_SCOPE]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_REFRESH_PURPOSE = "google-refresh-token"


class AuthError(RuntimeError):
    pass


def login_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(LOGIN_SCOPES),
            "access_type": "offline",  # refresh token for Classroom sync
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{params}"


def new_state() -> str:
    return secrets.token_urlsafe(32)


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        raise AuthError(f"token exchange failed: {e}") from e


def fetch_email(access_token: str) -> str:
    req = urllib.request.Request(
        USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            info = json.loads(resp.read().decode())
    except Exception as e:
        raise AuthError(f"userinfo failed: {e}") from e
    email = normalize_email(info.get("email") or "")
    if not email:
        raise AuthError("google did not return an email")
    if info.get("email_verified") is not True:
        raise AuthError("google account email is not verified")
    return email


def normalize_email(email: str) -> str:
    return email.strip().lower()


def refresh_token_for(user: User | None) -> str | None:
    """The user's decrypted Google refresh token, or None."""
    return unseal(_REFRESH_PURPOSE, getattr(user, "google_refresh_token", None))


def classroom_token_for(user: User | None) -> str | None:
    """The refresh token to act as `user` in Classroom: their own, else the
    shared GOOGLE_REFRESH_TOKEN, but only for GOOGLE_REFRESH_TOKEN_OWNER."""
    from app.moodle_tokens import is_owner

    own = refresh_token_for(user)
    if own:
        return own
    if settings.google_refresh_token and is_owner(user, settings.google_refresh_token_owner):
        return settings.google_refresh_token
    return None


def find_user(session: Session, email: str) -> User | None:
    """Case-insensitive, so rows stored before normalization still match
    (an exact, already-normalized row wins)."""
    email = normalize_email(email)
    return session.exec(select(User).where(User.email == email)).first() or session.exec(
        select(User).where(func.lower(User.email) == email)
    ).first()


def sign_in(
    session: Session, email: str, refresh_token: str | None = None
) -> User:
    """Upsert user by email, store Classroom refresh token, claim courses."""
    email = normalize_email(email)
    try:
        user = _upsert_user(session, email, refresh_token)
    except IntegrityError:
        # a concurrent first sign-in for this email inserted the row between
        # our lookup and commit; the retry finds and updates that row
        session.rollback()
        user = _upsert_user(session, email, refresh_token)
    if _may_claim_unowned(user):
        claim_unowned(session, user, source="moodle")
        session.refresh(user)
    return user


def _upsert_user(session: Session, email: str, refresh_token: str | None) -> User:
    user = find_user(session, email)
    if user is None:
        user = User(email=email)
    elif user.email != email:
        user.email = email
    if refresh_token and refresh_token_for(user) != refresh_token:
        user.google_refresh_token = seal(_REFRESH_PURPOSE, refresh_token)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def revoke_sessions(session: Session, user: User) -> None:
    """End every session `user` has, on every device: sessions carry the
    session_version they began with, and current_user rejects stale ones."""
    user.session_version += 1
    session.add(user)
    session.commit()


def claim_unowned(
    session: Session, user: User, source: str | None = None
) -> tuple[int, int]:
    """Give unowned (pre-auth) courses to `user`, optionally only from
    `source`. Returns (claimed, skipped): a course the user already has their
    own copy of (same source and id) is skipped, since (user_id, source,
    source_id) is unique."""
    have = set(session.exec(
        select(Course.source, Course.source_id).where(Course.user_id == user.id)
    ).all())
    q = select(Course).where(Course.user_id.is_(None))
    if source is not None:
        q = q.where(Course.source == source)
    claimed = skipped = 0
    for course in session.exec(q).all():
        if (course.source, course.source_id) in have:
            skipped += 1
            continue
        have.add((course.source, course.source_id))
        course.user_id = user.id
        session.add(course)
        claimed += 1
    session.commit()
    return claimed, skipped


def _may_claim_unowned(user: User) -> bool:
    """Courses with no owner were synced with the shared MOODLE_TOKEN before
    sign-in existed, so only that token's owner may claim them automatically.
    With no owner configured nobody does; use `app.admin_cli claim-unowned`."""
    from app.moodle_tokens import is_owner

    return is_owner(user, settings.moodle_token_owner)
