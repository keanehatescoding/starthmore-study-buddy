"""add resources.attempts/retry_after and quiz_attempts (issue #27)

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
    # every (chunk, attempt) quiz generation that ran, including empty ones
    op.create_table(
        "quiz_attempts",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chunk_id", "attempt"),
    )
    # Chunking used to fail a resource on its first empty reply, often a
    # truncated one; give those the bounded retries chunking now gets.
    op.execute(
        "UPDATE resources SET status = 'extracted', error = NULL "
        "WHERE status = 'failed' AND error = 'chunker produced no chunks'"
    )


def downgrade() -> None:
    op.drop_table("quiz_attempts")
    op.drop_column("resources", "retry_after")
    op.drop_column("resources", "attempts")
