"""Per-user Moodle tokens: obtain, verify, store encrypted, resolve.

A Moodle web-service token grants full access to that student's account, so
it is stored Fernet-encrypted (key derived from SECRET_KEY) and never logged.
Passwords are only ever held in memory for the single login/token.php call.

Resolution (`token_for`): the user's own connected token; otherwise the
global MOODLE_TOKEN, but only for MOODLE_TOKEN_OWNER.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

from app.config import settings
from app.crypto import seal, unseal
from app.moodle import MoodleClient, MoodleError

MOBILE_SERVICE = "moodle_mobile_app"
_PURPOSE = "moodle-token"


def encrypt_token(token: str) -> str:
    return seal(_PURPOSE, token)


def decrypt_token(stored: str | None) -> str | None:
    """Plain token, or None if missing/undecryptable (e.g. SECRET_KEY rotated)."""
    return unseal(_PURPOSE, stored)


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
    if not settings.moodle_token or not is_owner(user, settings.moodle_token_owner):
        return None
    return settings.moodle_token


def is_owner(user, owner_email: str) -> bool:
    """Whether `user` is the configured owner of a shared token (empty = nobody)."""
    owner = owner_email.strip().lower()
    return bool(owner) and user is not None and user.email.lower() == owner
