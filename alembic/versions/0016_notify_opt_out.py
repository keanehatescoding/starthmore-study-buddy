"""users.notify_email opt-out; notification_events.failed_reason (issue #56)

Revision ID: 0016
Revises: 0015_user_timezone
"""

import sqlalchemy as sa

from alembic import op

revision = "0016_notify_opt_out"
down_revision = "0015_user_timezone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column(
        "notify_email", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("notification_events",
                  sa.Column("failed_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("notification_events", "failed_reason")
    op.drop_column("users", "notify_email")
