"""MARSOUD-COMM-01 Phase C — add commissions column to payroll_lines

Adds a single Numeric(15,2) column. Can be negative when net carry-forward
clawbacks exceed earnings (the rep "owes" commission, the line subtracts
from their take-home).

Revision ID: ac_3f6d8b9a2c4
Revises: ab_2e7c4a9d5f1
Create Date: 2026-06-21
"""
from alembic import op
import sqlalchemy as sa

revision = 'ac_3f6d8b9a2c4'
down_revision = 'ab_2e7c4a9d5f1'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("payroll_lines", "commissions"):
        with op.batch_alter_table("payroll_lines", schema=None) as batch:
            batch.add_column(sa.Column("commissions", sa.Numeric(15, 2),
                                        nullable=True,
                                        server_default="0"))


def downgrade():
    if _has_col("payroll_lines", "commissions"):
        with op.batch_alter_table("payroll_lines", schema=None) as batch:
            batch.drop_column("commissions")
