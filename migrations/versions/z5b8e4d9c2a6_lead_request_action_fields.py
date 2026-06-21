"""MARSOUD-63 — add 2 optional Text fields to Lead

  leads.request_description   — وصف طلب العميل (long text, optional)
  leads.sales_action_required — المطلوب من السيلز (long text, optional)

Revision ID: z5b8e4d9c2a6
Revises: y4a7d3c1f5b9
Create Date: 2026-06-21
"""
from alembic import op
import sqlalchemy as sa

revision = 'z5b8e4d9c2a6'
down_revision = 'y4a7d3c1f5b9'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("leads", "request_description"):
        with op.batch_alter_table("leads", schema=None) as batch:
            batch.add_column(sa.Column("request_description", sa.Text(), nullable=True))
    if not _has_col("leads", "sales_action_required"):
        with op.batch_alter_table("leads", schema=None) as batch:
            batch.add_column(sa.Column("sales_action_required", sa.Text(), nullable=True))


def downgrade():
    if _has_col("leads", "sales_action_required"):
        with op.batch_alter_table("leads", schema=None) as batch:
            batch.drop_column("sales_action_required")
    if _has_col("leads", "request_description"):
        with op.batch_alter_table("leads", schema=None) as batch:
            batch.drop_column("request_description")
