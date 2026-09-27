"""lowercase user emails; encrypt stored Google refresh tokens

Revision ID: 0005
Revises: 0004_derived_cascade

Needs the same SECRET_KEY as the app: refresh tokens are sealed with it.
The sealing format is frozen here (not imported from app.crypto) so this
migration keeps writing what it wrote at the time, whatever the app does later.
"""

import base64
import hashlib
import os

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken
from dotenv import dotenv_values

from alembic import op

revision = "0005_normalize_auth"
down_revision = "0004_derived_cascade"
branch_labels = None
depends_on = None

_PREFIX = "v1:"
_PURPOSE = "google-refresh-token"


def _lookup(values, name: str):
    """Case-insensitive, like pydantic-settings' default."""
    return next((v for k, v in values.items() if k.lower() == name), None)


def _secret_key() -> str:
    """SECRET_KEY resolved like app.config at the time: env var, then .env
    in the working directory (parsed by python-dotenv, as pydantic-settings
    does), then the dev default."""
    for source in (os.environ, dotenv_values(".env")):
        value = _lookup(source, "secret_key")
        if value is not None:
            return value
    return "dev-insecure-change-me"


def _fernet() -> Fernet:
    digest = hashlib.sha256(f"{_PURPOSE}:{_secret_key()}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _seal(value: str) -> str:
    return _PREFIX + _fernet().encrypt(value.encode()).decode()


def _unseal(stored: str) -> str | None:
    try:
        return _fernet().decrypt(stored[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        return None


def upgrade() -> None:
    bind = op.get_bind()
    # Lowercase at most one row per address: if the lowercase form is taken,
    # or several mixed-case variants exist, the others stay as they are
    # (sign-in matches case-insensitively and prefers the lowercase row).
    # Done per row because a single UPDATE's NOT EXISTS can't see the rows
    # it lowercases itself, so two variants would collide on the unique index.
    taken = set()
    pending = []
    for user_id, email in bind.execute(
        sa.text("SELECT id, email FROM users ORDER BY created_at, id")
    ).all():
        if email == email.lower():
            taken.add(email)
        else:
            pending.append((user_id, email.lower()))
    for user_id, lowered in pending:
        if lowered in taken:
            continue
        taken.add(lowered)
        bind.execute(
            sa.text("UPDATE users SET email = :e WHERE id = :id"),
            {"e": lowered, "id": user_id},
        )
    rows = bind.execute(sa.text(
        "SELECT id, google_refresh_token FROM users "
        "WHERE google_refresh_token IS NOT NULL AND google_refresh_token NOT LIKE 'v1:%'"
    )).all()
    for user_id, token in rows:
        bind.execute(
            sa.text("UPDATE users SET google_refresh_token = :t WHERE id = :id"),
            {"t": _seal(token), "id": user_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, google_refresh_token FROM users WHERE google_refresh_token LIKE 'v1:%'"
    )).all()
    for user_id, stored in rows:
        plain = _unseal(stored)
        if plain is None:
            # Writing NULL would destroy the token for good; abort instead.
            raise RuntimeError(
                f"cannot decrypt refresh token for user {user_id}; wrong SECRET_KEY?"
            )
        bind.execute(
            sa.text("UPDATE users SET google_refresh_token = :t WHERE id = :id"),
            {"t": plain, "id": user_id},
        )
