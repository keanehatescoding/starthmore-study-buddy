"""users.timezone: per-user study day (issue #33)

Revision ID: 0015
Revises: 0014_assignment_topic_index
"""

import sqlalchemy as sa

from alembic import op

revision = "0015_user_timezone"
down_revision = "0014_assignment_topic_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("timezone", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "timezone")
