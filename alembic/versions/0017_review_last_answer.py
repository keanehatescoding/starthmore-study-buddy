"""review_states.last_answer, shown on the result page (issue #60)

Revision ID: 0017
Revises: 0016_notify_opt_out
"""

import sqlalchemy as sa

from alembic import op

revision = "0017_review_last_answer"
down_revision = "0016_notify_opt_out"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_states", sa.Column("last_answer", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("review_states", "last_answer")
