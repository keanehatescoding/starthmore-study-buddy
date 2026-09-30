"""retry Classroom Drive files that failed for want of a Drive scope

Revision ID: 0010
Revises: 0009_review_log
"""

from alembic import op

revision = "0010_retry_drive_files"
down_revision = "0009_review_log"
branch_labels = None
depends_on = None

# the error app.pipeline wrote before Drive downloads existed (issue #37)
NO_SCOPE_ERROR = "%needs a drive scope%"


def upgrade() -> None:
    op.execute(
        "UPDATE resources SET status = 'pending', error = NULL "
        "WHERE source = 'classroom' AND type = 'file' AND status = 'failed' "
        f"AND error LIKE '{NO_SCOPE_ERROR}'"
    )


def downgrade() -> None:
    pass  # pending rows just get extracted (or failed with a real error) next run
