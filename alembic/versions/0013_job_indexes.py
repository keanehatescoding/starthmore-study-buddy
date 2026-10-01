"""composite job indexes for claiming, reaping and pruning (issue #30)

Revision ID: 0013
Revises: 0012_session_version
"""

from alembic import op

revision = "0013_job_indexes"
down_revision = "0012_session_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_jobs_status_available_at", "jobs", ["status", "available_at"])
    op.create_index("ix_jobs_status_updated_at", "jobs", ["status", "updated_at"])
    # both lead with status, so the single-column index is redundant
    op.drop_index("ix_jobs_status", table_name="jobs")


def downgrade() -> None:
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.drop_index("ix_jobs_status_updated_at", table_name="jobs")
    op.drop_index("ix_jobs_status_available_at", table_name="jobs")
