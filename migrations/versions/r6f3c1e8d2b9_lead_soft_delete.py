"""MARSOUD-47 — soft-delete columns on leads

Adds:
  - leads.deleted_at       DateTime nullable
  - leads.deleted_by_id    Integer FK → users.id nullable

Revision ID: r6f3c1e8d2b9
Revises: q5e2b9c4f8a1
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa

revision = 'r6f3c1e8d2b9'
down_revision = 'q5e2b9c4f8a1'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("leads", "deleted_at"):
        with op.batch_alter_table("leads", schema=None) as batch_op:
            batch_op.add_column(sa.Column("deleted_at", sa.DateTime(),
                                           nullable=True))
    if not _has_col("leads", "deleted_by_id"):
        with op.batch_alter_table("leads", schema=None) as batch_op:
            batch_op.add_column(sa.Column("deleted_by_id", sa.Integer(),
                                           nullable=True))
            batch_op.create_foreign_key(
                "fk_leads_deleted_by_id", "users",
                ["deleted_by_id"], ["id"],
            )


def downgrade():
    if _has_col("leads", "deleted_by_id"):
        with op.batch_alter_table("leads", schema=None) as batch_op:
            try:
                batch_op.drop_constraint("fk_leads_deleted_by_id",
                                          type_="foreignkey")
            except Exception:
                pass
            batch_op.drop_column("deleted_by_id")
    if _has_col("leads", "deleted_at"):
        with op.batch_alter_table("leads", schema=None) as batch_op:
            batch_op.drop_column("deleted_at")
