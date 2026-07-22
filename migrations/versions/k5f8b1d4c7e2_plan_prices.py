"""MARSOUD-MULTI-CURRENCY-PRICING (Abdelhamid 2026-07-22).

Adds a per-currency price table hanging off Plan. Extensible for
USD/AED later — for now only EGP + SAR are required by the ticket.
The legacy Plan.price_monthly / price_yearly columns are kept as
the EGP fallback so nothing that reads them today breaks; new code
should call Plan.price_for(currency, cycle) which prefers a
plan_prices row and falls back to the legacy columns for EGP.

Revision ID: k5f8b1d4c7e2
Revises: j4e7a0c3d6b9
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'k5f8b1d4c7e2'
down_revision = 'j4e7a0c3d6b9'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "plan_prices" not in insp.get_table_names():
        op.create_table(
            "plan_prices",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plan_id", sa.Integer(),
                      sa.ForeignKey("plans.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("currency", sa.String(3), nullable=False),
            sa.Column("price_monthly", sa.Numeric(15, 2),
                      nullable=True),
            sa.Column("price_yearly", sa.Numeric(15, 2),
                      nullable=True),
            sa.UniqueConstraint("plan_id", "currency",
                                 name="uq_plan_prices_plan_currency"),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "plan_prices" in insp.get_table_names():
        op.drop_table("plan_prices")
