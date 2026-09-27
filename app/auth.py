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

from sqlmodel import Session, func, select

from app.classroom import SCOPES as CLASSROOM_SCOPES
from app.config import settings
from app.crypto import seal, unseal
from app.models import Course, User

LOGIN_SCOPES = ["openid", "email", "profile", *CLASSROOM_SCOPES]

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
    user = find_user(session, email)
    is_new = user is None
    if user is None:
        user = User(email=email)
    elif user.email != email:
        user.email = email
    if refresh_token and refresh_token_for(user) != refresh_token:
        user.google_refresh_token = seal(_REFRESH_PURPOSE, refresh_token)
    session.add(user)
    session.commit()
    session.refresh(user)
    if _may_claim_unowned(session, user, is_new):
        for course in session.exec(select(Course).where(Course.user_id.is_(None))).all():
            course.user_id = user.id
            session.add(course)
        session.commit()
        session.refresh(user)
    return user


def _may_claim_unowned(session: Session, user: User, is_new: bool) -> bool:
    """Courses with no owner were synced with the shared MOODLE_TOKEN before
    sign-in existed, so only that token's owner may claim them. Without a
    configured owner, only the very first account to sign in does."""
    owner = normalize_email(settings.moodle_token_owner)
    if owner:
        return user.email == owner
    if not is_new:
        return False
    return session.exec(select(func.count()).select_from(User)).one() == 1
