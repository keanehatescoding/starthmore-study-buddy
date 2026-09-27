"""lowercase user emails; encrypt stored Google refresh tokens

Revision ID: 0005
Revises: 0004_derived_cascade

Needs the same SECRET_KEY as the app: refresh tokens are sealed with it.
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_normalize_auth"
down_revision = "0004_derived_cascade"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.crypto import seal

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
            {"t": seal("google-refresh-token", token), "id": user_id},
        )


def downgrade() -> None:
    from app.crypto import unseal

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, google_refresh_token FROM users WHERE google_refresh_token LIKE 'v1:%'"
    )).all()
    for user_id, stored in rows:
        plain = unseal("google-refresh-token", stored)
        if plain is None:
            # Writing NULL would destroy the token for good; abort instead.
            raise RuntimeError(
                f"cannot decrypt refresh token for user {user_id}; wrong SECRET_KEY?"
            )
        bind.execute(
            sa.text("UPDATE users SET google_refresh_token = :t WHERE id = :id"),
            {"t": plain, "id": user_id},
        )
