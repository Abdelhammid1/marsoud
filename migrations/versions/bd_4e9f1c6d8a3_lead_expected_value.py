"""MARSOUD-DASH-01 — add expected_value to Lead

  leads.expected_value — Numeric(15,2) optional, the deal size the sales
  rep expects if the lead converts. Summed across open leads on the
  owner dashboard's "potential pipeline" card.

Revision ID: bd_4e9f1c6d8a3
Revises: ac_3f6d8b9a2c4
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa

revision = 'bd_4e9f1c6d8a3'
down_revision = 'ac_3f6d8b9a2c4'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("leads", "expected_value"):
        with op.batch_alter_table("leads", schema=None) as batch:
            batch.add_column(sa.Column("expected_value",
                                       sa.Numeric(15, 2),
                                       nullable=True))


def downgrade():
    if _has_col("leads", "expected_value"):
        with op.batch_alter_table("leads", schema=None) as batch:
            batch.drop_column("expected_value")
