"""index assignments.topic_id (issue #33)

Revision ID: 0014
Revises: 0013_job_indexes
"""

from alembic import op

revision = "0014_assignment_topic_index"
down_revision = "0013_job_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_assignments_topic_id", "assignments", ["topic_id"])


def downgrade() -> None:
    op.drop_index("ix_assignments_topic_id", table_name="assignments")
