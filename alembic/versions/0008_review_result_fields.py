"""add review_states.first_answered_at (daily new-item cap) and last_feedback

Revision ID: 0008
Revises: 0007_notify_batch_key
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_review_result_fields"
down_revision = "0007_notify_batch_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "review_states",
        sa.Column("first_answered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("review_states", sa.Column("last_feedback", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_states", "last_feedback")
    op.drop_column("review_states", "first_answered_at")
