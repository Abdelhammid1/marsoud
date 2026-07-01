"""MARSOUD-PARTIAL-SETTLE — partial payments on employee accruals.

Adds employee_accruals.paid_amount so a single accrual can be settled
across multiple partial cash payments. When paid_amount reaches amount,
the existing settled_at timestamp fires. All existing accruals are
backfilled: settled rows get paid_amount = amount, unsettled rows
get paid_amount = 0.

Revision ID: c6_e8a4b2f7c3d
Revises: c5_d3f4e8a7b1c
"""
from alembic import op
import sqlalchemy as sa

revision = "c6_e8a4b2f7c3d"
down_revision = "c5_d3f4e8a7b1c"
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("employee_accruals", "paid_amount"):
        with op.batch_alter_table("employee_accruals") as batch:
            batch.add_column(sa.Column(
                "paid_amount", sa.Numeric(15, 2),
                nullable=False, server_default="0",
            ))
        # Backfill: fully-settled accruals get paid_amount = amount so
        # remaining reads as 0. Unsettled stay at 0 (still owed).
        op.execute(
            "UPDATE employee_accruals "
            "SET paid_amount = amount "
            "WHERE settled_at IS NOT NULL"
        )


def downgrade():
    if _has_col("employee_accruals", "paid_amount"):
        with op.batch_alter_table("employee_accruals") as batch:
            batch.drop_column("paid_amount")
