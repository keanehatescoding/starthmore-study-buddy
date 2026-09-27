"""Per-user Moodle tokens: obtain, verify, store encrypted, resolve.

A Moodle web-service token grants full access to that student's account, so
it is stored Fernet-encrypted (key derived from SECRET_KEY) and never logged.
Passwords are only ever held in memory for the single login/token.php call.

Resolution (`token_for`): the user's own connected token; otherwise the
global MOODLE_TOKEN, but only for MOODLE_TOKEN_OWNER (or for everyone when
that is unset — the single-user setup this app started as).
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
import urllib.request

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.moodle import MoodleClient, MoodleError

MOBILE_SERVICE = "moodle_mobile_app"
_PREFIX = "v1:"


def _fernet() -> Fernet:
    digest = hashlib.sha256(b"moodle-token:" + settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_token(token: str) -> str:
    return _PREFIX + _fernet().encrypt(token.encode()).decode()


def decrypt_token(stored: str | None) -> str | None:
    """Plain token, or None if missing/undecryptable (e.g. SECRET_KEY rotated)."""
    if not stored or not stored.startswith(_PREFIX):
        return None
    try:
        return _fernet().decrypt(stored[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        return None


def fetch_token(base_url: str, username: str, password: str, timeout: int = 30) -> str:
    """Exchange Moodle credentials for a mobile-service token (login/token.php).

    Credentials go in the POST body, never the URL. Errors carry Moodle's
    message but never the password.
    """
    data = urllib.parse.urlencode({
        "username": username, "password": password, "service": MOBILE_SERVICE,
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/login/token.php", data=data, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except (OSError, ValueError) as e:  # URLError/timeouts, non-JSON reply
        raise MoodleError(f"could not reach Moodle: {type(e).__name__}") from None
    token = body.get("token") if isinstance(body, dict) else None
    if not token:
        message = body.get("error") if isinstance(body, dict) else None
        raise MoodleError(message or "Moodle did not return a token")
    return token


def verify_token(base_url: str, token: str) -> dict:
    """Site info for a token (raises MoodleError if Moodle rejects it)."""
    return MoodleClient(base_url, token).site_info()


def token_for(user) -> str | None:
    """The Moodle token to act as `user`, or None if they have none."""
    own = decrypt_token(getattr(user, "moodle_token", None))
    if own:
        return own
    if not settings.moodle_token:
        return None
    owner = settings.moodle_token_owner.strip().lower()
    if owner and (user is None or user.email.lower() != owner):
        return None
    return settings.moodle_token
