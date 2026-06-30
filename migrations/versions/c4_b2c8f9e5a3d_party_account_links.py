"""MARSOUD-COA-REBUILD — subsidiary ledger: link parties to accounts.

Adds nullable `account_id` FK to customers, vendors, and employees so
each party can own a sub-account under the relevant header (1130 / 2110
/ 2130). All existing rows stay NULL until services/subsidiary.py
backfills them on first invoice / bill / payroll, or the explicit
backfill helper is run.

Revision ID: c4_b2c8f9e5a3d
Revises: c3_a1b7e8d4f2c
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision = "c4_b2c8f9e5a3d"
down_revision = "c3_a1b7e8d4f2c"
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    for table in ("customers", "vendors", "employees"):
        if not _has_col(table, "account_id"):
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column(
                    "account_id", sa.Integer(),
                    sa.ForeignKey(
                        "accounts.id",
                        name=f"fk_{table}_account_id_accounts",
                    ),
                    nullable=True,
                ))


def downgrade():
    for table in ("customers", "vendors", "employees"):
        if _has_col(table, "account_id"):
            with op.batch_alter_table(table) as batch:
                batch.drop_column("account_id")
