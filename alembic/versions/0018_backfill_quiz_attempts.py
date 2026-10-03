"""backfill quiz_attempts from quiz items generated before 0011 (issue #64)

The quiz stage now picks chunks with one NOT EXISTS (quiz_attempts) query, so
a chunk quizzed before quiz_attempts existed needs its marker or it would be
quizzed again.

Revision ID: 0018
Revises: 0017_review_last_answer
"""

import sqlalchemy as sa

from alembic import op

revision = "0018_backfill_quiz_attempts"
down_revision = "0017_review_last_answer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    done = {(str(c), a) for c, a in conn.execute(
        sa.text("SELECT chunk_id, attempt FROM quiz_attempts"))}
    # generation_key is "<chunk_id>:<attempt>" or "<chunk_id>:<attempt>:<i>"
    missing = set()
    for chunk_id, key in conn.execute(
            sa.text("SELECT chunk_id, generation_key FROM quiz_items")):
        parts = key.split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        if (str(chunk_id), int(parts[1])) not in done:
            missing.add((chunk_id, int(parts[1])))
    if missing:
        conn.execute(
            sa.text("INSERT INTO quiz_attempts (chunk_id, attempt) VALUES (:c, :a)"),
            [{"c": c, "a": a} for c, a in sorted(missing, key=str)],
        )


def downgrade() -> None:
    pass  # the markers are correct under 0017 as well
