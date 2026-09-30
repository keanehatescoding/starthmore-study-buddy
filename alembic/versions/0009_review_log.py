"""add review_logs: one row per graded answer, backfilled from review_states

Revision ID: 0009
Revises: 0008_review_result_fields
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_review_log"
down_revision = "0008_review_result_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("quiz_item_id", sa.Uuid(), nullable=True),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("partial_credit", sa.Float(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["quiz_item_id"], ["quiz_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_logs_user_answered", "review_logs", ["user_id", "answered_at"])
    # Only each item's latest answer survives in review_states; keep that much
    # history. Reusing the state's id keeps this a single INSERT ... SELECT.
    op.execute(
        "INSERT INTO review_logs (id, user_id, quiz_item_id, verdict, answered_at) "
        "SELECT id, user_id, quiz_item_id, last_result, answered_at FROM review_states "
        "WHERE answered_at IS NOT NULL AND last_result IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_review_logs_user_answered", table_name="review_logs")
    op.drop_table("review_logs")
