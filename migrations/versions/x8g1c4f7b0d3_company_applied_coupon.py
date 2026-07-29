"""MARSOUD-DISCOUNT-COUPONS wiring (Abdelhamid 2026-07-29).

Nullable FK on companies pointing at the coupon the owner
entered on /choose-plan. Used by SaaS billing (next ticket) to
apply the discount to the FIRST subscription invoice + call
coupons.redeem() after payment succeeds. NULL until the owner
picks one; cleared after successful redemption.

Revision ID: x8g1c4f7b0d3
Revises: w7f0b3e6d9a2
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa


revision = 'x8g1c4f7b0d3'
down_revision = 'w7f0b3e6d9a2'
branch_labels = None
depends_on = None


def _has_col(insp, table, col):
    return any(c["name"] == col for c in insp.get_columns(table))


def upgrade():
    insp = sa.inspect(op.get_bind())
    if not _has_col(insp, "companies", "applied_coupon_id"):
        with op.batch_alter_table("companies") as batch:
            batch.add_column(sa.Column(
                "applied_coupon_id", sa.Integer(),
                sa.ForeignKey("coupons.id",
                              name="fk_companies_applied_coupon_id"),
                nullable=True))


def downgrade():
    insp = sa.inspect(op.get_bind())
    if _has_col(insp, "companies", "applied_coupon_id"):
        with op.batch_alter_table("companies") as batch:
            batch.drop_column("applied_coupon_id")
