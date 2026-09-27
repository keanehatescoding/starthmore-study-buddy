"""ON DELETE CASCADE for resource -> chunk -> quiz item -> review state

Revision ID: 0004
Revises: 0003_course_owner
"""

from alembic import op

revision = "0004_derived_cascade"
down_revision = "0003_course_owner"
branch_labels = None
depends_on = None

# (table, column, referred table); 0001 left these unnamed, so Postgres
# assigned its default <table>_<column>_fkey names.
FKS = [
    ("chunks", "resource_id", "resources"),
    ("quiz_items", "chunk_id", "chunks"),
    ("review_states", "quiz_item_id", "quiz_items"),
]


def _recreate(ondelete: str | None) -> None:
    for table, column, referred in FKS:
        name = f"{table}_{column}_fkey"
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(
            name, table, referred, [column], ["id"], ondelete=ondelete
        )


def upgrade() -> None:
    _recreate("CASCADE")


def downgrade() -> None:
    _recreate(None)
