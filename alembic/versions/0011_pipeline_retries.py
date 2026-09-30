"""add resources.attempts/retry_after and chunks.quiz_attempt (issue #27)

Revision ID: 0011
Revises: 0010_retry_drive_files
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_pipeline_retries"
down_revision = "0010_retry_drive_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resources", sa.Column("attempts", sa.Integer(), nullable=False,
                                         server_default="0"))
    op.add_column("resources", sa.Column("retry_after", sa.DateTime(timezone=True),
                                         nullable=True))
    op.add_column("chunks", sa.Column("quiz_attempt", sa.Integer(), nullable=True))
    # Chunking used to fail a resource on its first empty reply, often a
    # truncated one; give those the bounded retries chunking now gets.
    op.execute(
        "UPDATE resources SET status = 'extracted', error = NULL "
        "WHERE status = 'failed' AND error = 'chunker produced no chunks'"
    )


def downgrade() -> None:
    op.drop_column("chunks", "quiz_attempt")
    op.drop_column("resources", "retry_after")
    op.drop_column("resources", "attempts")
