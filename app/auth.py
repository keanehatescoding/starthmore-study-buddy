"""Google sign-in. One consent grants login (openid/email) AND Classroom sync.

Flow: /login -> Google (with state) -> /auth/callback -> exchange code for
tokens -> fetch email -> upsert User (storing the refresh token for
Classroom) -> claim unowned courses -> session cookie.
"""

from __future__ import annotations

import json
import secrets
import urllib.parse
import urllib.request

from sqlmodel import Session, select

from app.classroom import SCOPES as CLASSROOM_SCOPES
from app.models import Course, User

LOGIN_SCOPES = ["openid", "email", "profile", *CLASSROOM_SCOPES]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


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
    email = info.get("email")
    if not email:
        raise AuthError("google did not return an email")
    return email


def sign_in(
    session: Session, email: str, refresh_token: str | None = None
) -> User:
    """Upsert user by email, store Classroom refresh token, claim courses."""
    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        user = User(email=email, google_refresh_token=refresh_token)
        session.add(user)
        session.commit()
        session.refresh(user)
    elif refresh_token and user.google_refresh_token != refresh_token:
        user.google_refresh_token = refresh_token
        session.add(user)
        session.commit()
    # first login claims courses synced before auth existed
    for course in session.exec(select(Course).where(Course.user_id.is_(None))).all():
        course.user_id = user.id
        session.add(course)
    session.commit()
    session.refresh(user)
    return user
