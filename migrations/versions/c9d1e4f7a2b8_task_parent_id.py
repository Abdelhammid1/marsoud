"""MARSOUD-PARENT-CHILD-TASK-HIERARCHY (2026-08-09) — task.parent_task_id.

Adds an optional self-FK so any Task can be a subtask of any other
task in the same company. Same-company + no-cycle rules are
enforced in app/services/task_hierarchy.py::validate_parent (backend
authority per ticket). Deleting a parent nulls out subtasks'
parent_task_id (ondelete=SET NULL); services/tasks_extras.py::
delete_task_fully also does the UPDATE explicitly so SQLite (where
PRAGMA foreign_keys defaults to OFF) doesn't leave dangling FKs.

Revision ID: c9d1e4f7a2b8
Revises: b8f4d1e2a5c7
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa


revision = "c9d1e4f7a2b8"
down_revision = "b8f4d1e2a5c7"
branch_labels = None
depends_on = None


TABLE = "tasks"
COL = "parent_task_id"
FK = "fk_tasks_parent_task_id"
IDX = "ix_tasks_parent_task_id"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_col(table, col):
    return col in {c["name"] for c in _inspector().get_columns(table)}


def _has_index(table, name):
    return any(ix["name"] == name for ix in _inspector().get_indexes(table))


def upgrade():
    if not _has_col(TABLE, COL):
        with op.batch_alter_table(TABLE, schema=None) as batch:
            batch.add_column(sa.Column(COL, sa.Integer(), nullable=True))
            # batch_alter_table takes care of recreating the table on
            # SQLite; add the FK inside the batch so it lands in the
            # rebuild.
            batch.create_foreign_key(
                FK, TABLE, [COL], ["id"], ondelete="SET NULL",
            )
    if _has_col(TABLE, COL) and not _has_index(TABLE, IDX):
        op.create_index(IDX, TABLE, [COL])


def downgrade():
    if _has_col(TABLE, COL):
        try:
            op.drop_index(IDX, table_name=TABLE)
        except Exception:
            pass
        with op.batch_alter_table(TABLE, schema=None) as batch:
            try:
                batch.drop_constraint(FK, type_="foreignkey")
            except Exception:
                pass
            batch.drop_column(COL)
