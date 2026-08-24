"""MARSOUD-COMM-SETTLE: settled_amount + settled_at on sales_commissions

Revision ID: c68a8e80606b
Revises: e174eaea899b
Create Date: 2026-08-25 00:32:59.494586

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c68a8e80606b'
down_revision = 'e174eaea899b'
branch_labels = None
depends_on = None


def upgrade():
    # Two new columns on sales_commissions so a commission can be settled
    # in parts (payroll now, cash later) instead of only UNPAID -> PAID.
    with op.batch_alter_table("sales_commissions") as batch:
        batch.add_column(sa.Column(
            "settled_amount", sa.Numeric(15, 4),
            nullable=False, server_default="0"))
        batch.add_column(sa.Column("settled_at", sa.DateTime(), nullable=True))

    # Backfill so the new column agrees with the state already recorded in
    # `status`. This is NOT an accounting correction — it writes no journal
    # and moves no balance. It only says "this row that is already marked
    # PAID was settled for its full amount", which is what PAID meant
    # before the column existed. Rows still UNPAID keep settled_amount 0.
    op.execute(
        "UPDATE sales_commissions "
        "SET settled_amount = amount, settled_at = created_at "
        "WHERE status = 'PAID'"
    )


def downgrade():
    with op.batch_alter_table("sales_commissions") as batch:
        batch.drop_column("settled_at")
        batch.drop_column("settled_amount")
