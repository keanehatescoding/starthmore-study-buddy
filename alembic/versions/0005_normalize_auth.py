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
    # Skip rows whose lowercase form already exists (a duplicate from before
    # normalization); sign-in matches case-insensitively and prefers that row.
    bind.execute(sa.text(
        "UPDATE users SET email = lower(email) WHERE email <> lower(email) "
        "AND NOT EXISTS (SELECT 1 FROM users u2 WHERE u2.email = lower(users.email))"
    ))
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
        bind.execute(
            sa.text("UPDATE users SET google_refresh_token = :t WHERE id = :id"),
            {"t": unseal("google-refresh-token", stored), "id": user_id},
        )
