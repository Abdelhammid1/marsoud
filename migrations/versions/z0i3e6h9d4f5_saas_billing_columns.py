"""MARSOUD-SAAS-BILLING-01 (Batch 5 Ticket 7, 2026-07-29).

3 additive columns on companies for the end-to-end SaaS billing
loop:

  · subscription_frequency  — 'MONTHLY' | 'YEARLY' | NULL. Chosen
    on /choose-plan alongside the plan_id. Drives the next-
    invoice date (monthly = 3d before expiry, yearly = 30d
    before).

  · saas_customer_id        — nullable FK to customers. Represents
    THIS company's mirror in Manasty's own CRM (auto-created lazily
    by saas_billing.ensure_saas_customer on the first SaaS
    invoice).

  · price_lock              — Numeric(15,2) nullable. When set,
    this company's NEXT invoice uses this price regardless of
    the plan's current price. Set by super-admin on the
    /admin/saas page. Protects existing customers from plan-price
    changes ("لو سعر باقة اتغير، العميل الحالي ما يتأثرش
    أوتوماتيك").

applied_coupon_id was added in Ticket 4's migration
(x8g1c4f7b0d3) so this migration only carries the remaining
three columns.

Revision ID: z0i3e6h9d4f5
Revises: y9h2d5g8c1e4
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa


revision = 'z0i3e6h9d4f5'
down_revision = 'y9h2d5g8c1e4'
branch_labels = None
depends_on = None


def _has_col(insp, table, col):
    return any(c["name"] == col for c in insp.get_columns(table))


def upgrade():
    insp = sa.inspect(op.get_bind())
    with op.batch_alter_table("companies") as batch:
        if not _has_col(insp, "companies", "subscription_frequency"):
            batch.add_column(sa.Column(
                "subscription_frequency", sa.String(10),
                nullable=True))
        if not _has_col(insp, "companies", "saas_customer_id"):
            batch.add_column(sa.Column(
                "saas_customer_id", sa.Integer(),
                sa.ForeignKey("customers.id",
                              name="fk_companies_saas_customer_id"),
                nullable=True))
        if not _has_col(insp, "companies", "price_lock"):
            batch.add_column(sa.Column(
                "price_lock", sa.Numeric(15, 2), nullable=True))


def downgrade():
    insp = sa.inspect(op.get_bind())
    with op.batch_alter_table("companies") as batch:
        for col in ("price_lock", "saas_customer_id",
                     "subscription_frequency"):
            if _has_col(insp, "companies", col):
                batch.drop_column(col)
