"""At most one pending/running send_notifications job

Revision ID: 0006
Revises: 0005_normalize_auth
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_active_notify_job"
down_revision = "0005_normalize_auth"
branch_labels = None
depends_on = None

# Frozen copy of app.models.ACTIVE_NOTIFY_WHERE.
WHERE = "type = 'send_notifications' AND status IN ('pending', 'running')"


def upgrade() -> None:
    # Nothing enqueued notify jobs before this revision, but fail any stray
    # duplicates rather than let the index creation abort the deploy.
    op.execute(f"""
        UPDATE jobs SET status = 'failed', error = 'superseded duplicate notify job'
        WHERE {WHERE} AND id NOT IN (
            SELECT id FROM jobs WHERE {WHERE} ORDER BY created_at LIMIT 1
        )
    """)
    op.create_index(
        "uq_jobs_active_notify", "jobs", ["type"], unique=True,
        postgresql_where=sa.text(WHERE), sqlite_where=sa.text(WHERE),
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_active_notify", table_name="jobs")
