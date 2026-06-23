"""MARSOUD-TASK-ARCHIVE-01 — soft archive for completed tasks.

  tasks.archived_at      DateTime nullable
  tasks.archived_by_id   FK users.id nullable

The task itself stays in the DB with every comment, attachment, and
activity log row intact — the archive is a soft flag that hides it
from the Kanban + dashboards. Restoring just NULLs the columns.

Revision ID: c0_7d3b9e2f4a1
Revises: bf_6a2c8d4e5f9
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision = 'c0_7d3b9e2f4a1'
down_revision = 'bf_6a2c8d4e5f9'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def _has_index(table, name):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return name in {i["name"] for i in insp.get_indexes(table)}


def upgrade():
    if not _has_col("tasks", "archived_at"):
        with op.batch_alter_table("tasks", schema=None) as batch:
            batch.add_column(sa.Column("archived_at", sa.DateTime(),
                                       nullable=True))
    if not _has_col("tasks", "archived_by_id"):
        with op.batch_alter_table("tasks", schema=None) as batch:
            batch.add_column(sa.Column(
                "archived_by_id", sa.Integer(), nullable=True,
            ))
    # Standalone index creation (idempotent — guarded by _has_index)
    if not _has_index("tasks", "ix_tasks_archived_at"):
        op.create_index("ix_tasks_archived_at", "tasks", ["archived_at"])


def downgrade():
    if _has_index("tasks", "ix_tasks_archived_at"):
        op.drop_index("ix_tasks_archived_at", table_name="tasks")
    if _has_col("tasks", "archived_by_id"):
        with op.batch_alter_table("tasks", schema=None) as batch:
            batch.drop_column("archived_by_id")
    if _has_col("tasks", "archived_at"):
        with op.batch_alter_table("tasks", schema=None) as batch:
            batch.drop_column("archived_at")
