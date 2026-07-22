"""MARSOUD-DISCOUNT-COUPONS (Abdelhamid 2026-07-22).

coupons + coupon_redemptions tables. Coupons run promo codes at
signup / renewal without touching plan prices. Redemptions track
who used what so max_uses + max_uses_per_customer enforce.

Revision ID: o9d2f5a8c1e4
Revises: n8c1e4f7b0d3
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'o9d2f5a8c1e4'
down_revision = 'n8c1e4f7b0d3'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "coupons" not in insp.get_table_names():
        op.create_table(
            "coupons",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(40), nullable=False,
                      unique=True, index=True),
            sa.Column("discount_type", sa.String(10),
                      nullable=False),   # PERCENT | FIXED
            sa.Column("discount_value", sa.Numeric(15, 2),
                      nullable=False),
            sa.Column("valid_from", sa.Date(), nullable=True),
            sa.Column("valid_until", sa.Date(), nullable=True),
            sa.Column("max_uses", sa.Integer(), nullable=True),
            sa.Column("max_uses_per_customer", sa.Integer(),
                      nullable=False, server_default="1"),
            sa.Column("applies_to_plan_ids", sa.Text(),
                      nullable=True),   # JSON list, NULL = all
            sa.Column("active", sa.Boolean(),
                      nullable=False, server_default="1"),
            sa.Column("created_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
    if "coupon_redemptions" not in insp.get_table_names():
        op.create_table(
            "coupon_redemptions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("coupon_id", sa.Integer(),
                      sa.ForeignKey("coupons.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("amount_saved", sa.Numeric(15, 2),
                      nullable=False),
            sa.Column("redeemed_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    for tname in ("coupon_redemptions", "coupons"):
        if tname in insp.get_table_names():
            op.drop_table(tname)
