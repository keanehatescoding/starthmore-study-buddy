"""add courses.user_id for per-user scoping (auth)

Revision ID: 0003
Revises: 0002_answered_at
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_course_owner"
down_revision = "0002_answered_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("courses", sa.Column("user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_courses_user", "courses", "users", ["user_id"], ["id"])
    op.create_index("ix_courses_user_id", "courses", ["user_id"])
    op.drop_constraint("uq_courses_source", "courses", type_="unique")
    op.create_unique_constraint(
        "uq_courses_user_source", "courses", ["user_id", "source", "source_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_courses_user_source", "courses", type_="unique")
    op.create_unique_constraint("uq_courses_source", "courses", ["source", "source_id"])
    op.drop_index("ix_courses_user_id", table_name="courses")
    op.drop_constraint("fk_courses_user", "courses", type_="foreignkey")
    op.drop_column("courses", "user_id")
