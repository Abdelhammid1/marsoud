"""MARSOUD-COA-REBUILD — header vs leaf accounts.

Adds `accounts.is_postable` so the chart of accounts can model parent
"header" rows that exist only for grouping + reporting. post_journal
refuses to write a line on any account where is_postable=False.

Default for every existing row = True (safest: keep current postings
working). The new seed (replace-CoA migration) will flip the headers
to False explicitly.

Revision ID: c3_a1b7e8d4f2c
Revises: c2_9f5b3e7d2a8
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision = "c3_a1b7e8d4f2c"
down_revision = "c2_9f5b3e7d2a8"
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("accounts", "is_postable"):
        with op.batch_alter_table("accounts") as batch:
            batch.add_column(sa.Column(
                "is_postable", sa.Boolean(), nullable=False,
                server_default="1",
            ))


def downgrade():
    if _has_col("accounts", "is_postable"):
        with op.batch_alter_table("accounts") as batch:
            batch.drop_column("is_postable")
