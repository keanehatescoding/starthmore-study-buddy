"""add users.session_version for server-side session revocation (issue #29)

Revision ID: 0012
Revises: 0011_pipeline_retries
"""

import sqlalchemy as sa

from alembic import op

revision = "0012_session_version"
down_revision = "0011_pipeline_retries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False,
                                     server_default="0"))


def downgrade() -> None:
    op.drop_column("users", "session_version")
