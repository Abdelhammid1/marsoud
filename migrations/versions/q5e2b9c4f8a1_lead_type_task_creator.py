"""MARSOUD-46 + MARSOUD-44 — Lead Type column + Task creator FK

Adds:
  - leads.lead_type       String(20)  nullable  — Inbound / Outbound / Referral / Existing
  - tasks.created_by_id   Integer FK → users.id nullable, backfilled to assigned_to_id

leads.source stays as String — same column, new constrained values written by
the form. Old free-text rows render as-is.

Revision ID: q5e2b9c4f8a1
Revises: f6f7c2f44dd3
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa

revision = 'q5e2b9c4f8a1'
down_revision = 'f6f7c2f44dd3'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("leads", "lead_type"):
        with op.batch_alter_table("leads", schema=None) as batch_op:
            batch_op.add_column(sa.Column("lead_type", sa.String(length=20),
                                           nullable=True))

    if not _has_col("tasks", "created_by_id"):
        with op.batch_alter_table("tasks", schema=None) as batch_op:
            batch_op.add_column(sa.Column("created_by_id", sa.Integer(),
                                           nullable=True))
            batch_op.create_foreign_key(
                "fk_tasks_created_by_id", "users",
                ["created_by_id"], ["id"],
            )
        # Backfill: best-guess that the assignee was also the creator
        # (every existing task has assigned_to_id NOT NULL).
        op.execute("UPDATE tasks SET created_by_id = assigned_to_id "
                   "WHERE created_by_id IS NULL")


def downgrade():
    if _has_col("tasks", "created_by_id"):
        with op.batch_alter_table("tasks", schema=None) as batch_op:
            try:
                batch_op.drop_constraint("fk_tasks_created_by_id",
                                          type_="foreignkey")
            except Exception:
                pass
            batch_op.drop_column("created_by_id")
    if _has_col("leads", "lead_type"):
        with op.batch_alter_table("leads", schema=None) as batch_op:
            batch_op.drop_column("lead_type")
