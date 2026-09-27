"""Encryption at rest for credentials stored in the database.

Each purpose derives its own Fernet key from SECRET_KEY, so rotating
SECRET_KEY makes every stored secret undecryptable (the user reconnects).
Stored values carry a version prefix; anything without it is not trusted.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_PREFIX = "v1:"


def _fernet(purpose: str) -> Fernet:
    digest = hashlib.sha256(f"{purpose}:".encode() + settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def seal(purpose: str, value: str) -> str:
    return _PREFIX + _fernet(purpose).encrypt(value.encode()).decode()


def unseal(purpose: str, stored: str | None) -> str | None:
    """Plain value, or None if missing/undecryptable (e.g. SECRET_KEY rotated)."""
    if not stored or not stored.startswith(_PREFIX):
        return None
    try:
        return _fernet(purpose).decrypt(stored[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        return None
