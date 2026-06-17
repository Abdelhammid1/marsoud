"""MARSOUD-58 — per-section sub-item gating

Adds plans.allowed_subitems JSON column. NULL means "back-compat: all
sub-items allowed" so existing companies don't get anything locked
unexpectedly.

Seeds default sub-items for the 3 built-in plans:
  basic         — core sub-items only (invoices, customers, journals,
                  accounts, payment_methods, reports, dashboard, settings)
  professional  — basic + intermediate (pos, products, inventory,
                  warehouses, vendor_bills, vendors, assets)
  enterprise    — null (= everything allowed)

Revision ID: y4a7d3c1f5b9
Revises: x3f6e9a4d1c8
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa
import json

revision = 'y4a7d3c1f5b9'
down_revision = 'x3f6e9a4d1c8'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


# Default catalogs per built-in plan. Kept here (instead of importing from
# the Python module) so re-running the migration on a fresh DB always
# yields the same starting state even if the catalog later evolves.
BASIC_SUBITEMS = [
    "dashboard.index",
    "invoices.index", "customers.index",
    "journals.index", "accounts.index",
    "reports.index",
    "settings_roles.index", "payment_methods.index",
    "companies.edit", "audit_log.index",
]
PROFESSIONAL_SUBITEMS = BASIC_SUBITEMS + [
    "pos.index", "products.index",
    "inventory.index", "inventory.warehouses",
    "vendor_bills.index", "vendors.index",
    "assets.index",
]
# enterprise stays NULL → no sub-item filter applied → everything visible.


def upgrade():
    if not _has_col("plans", "allowed_subitems"):
        with op.batch_alter_table("plans", schema=None) as batch:
            batch.add_column(sa.Column("allowed_subitems", sa.Text(),
                                        nullable=True))

    conn = op.get_bind()
    for code, items in [
        ("basic", BASIC_SUBITEMS),
        ("professional", PROFESSIONAL_SUBITEMS),
    ]:
        conn.execute(sa.text(
            "UPDATE plans SET allowed_subitems = :items "
            "WHERE code = :code AND (allowed_subitems IS NULL "
            "OR allowed_subitems = '')"
        ), {"items": json.dumps(items), "code": code})
    # Enterprise stays NULL.


def downgrade():
    if _has_col("plans", "allowed_subitems"):
        with op.batch_alter_table("plans", schema=None) as batch:
            batch.drop_column("allowed_subitems")
