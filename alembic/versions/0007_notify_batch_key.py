"""Persisted Resend idempotency key per notification batch

Revision ID: 0007
Revises: 0006_active_notify_job
"""

import sqlalchemy as sa

from alembic import op

revision = "0007_notify_batch_key"
down_revision = "0006_active_notify_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notification_events", sa.Column("batch_key", sa.String(), nullable=True))
    op.create_index("ix_notification_events_batch_key", "notification_events", ["batch_key"])


def downgrade() -> None:
    op.drop_index("ix_notification_events_batch_key", table_name="notification_events")
    op.drop_column("notification_events", "batch_key")
