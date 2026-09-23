"""add review_states.answered_at (Phase 5 streak/stats)

Revision ID: 0002
Revises: 0001_initial
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_answered_at"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "review_states", sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("review_states", "answered_at")
