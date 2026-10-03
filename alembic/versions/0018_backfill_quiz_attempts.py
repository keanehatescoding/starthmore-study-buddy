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


# a page's distinct chunk ids go into one IN list; stay under SQLite's
# 999-variable limit on builds older than 3.32
BATCH = 500


def _attempt(key: str) -> int | None:
    # generation_key is "<chunk_id>:<attempt>" or "<chunk_id>:<attempt>:<i>"
    parts = key.split(":")
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None


def upgrade() -> None:
    """Pages through quiz_items by id so memory stays bounded by BATCH."""
    conn = op.get_bind()
    first = sa.text("SELECT id, chunk_id, generation_key FROM quiz_items "
                    "ORDER BY id LIMIT :n")
    after = sa.text("SELECT id, chunk_id, generation_key FROM quiz_items "
                    "WHERE id > :last ORDER BY id LIMIT :n")
    marked = sa.text("SELECT chunk_id, attempt FROM quiz_attempts "
                     "WHERE chunk_id IN :ids").bindparams(sa.bindparam("ids", expanding=True))
    insert = sa.text("INSERT INTO quiz_attempts (chunk_id, attempt) VALUES (:c, :a)")
    last = None
    while rows := conn.execute(after if last else first, {"last": last, "n": BATCH}).all():
        last = rows[-1][0]
        pairs = {(c, a) for _, c, key in rows if (a := _attempt(key)) is not None}
        if not pairs:
            continue
        # markers inserted for earlier pages are visible here too
        done = {(c, a) for c, a in conn.execute(marked, {"ids": list({c for c, _ in pairs})})}
        missing = pairs - done
        if missing:
            conn.execute(insert, [{"c": c, "a": a} for c, a in missing])


def downgrade() -> None:
    pass  # the markers are correct under 0017 as well
