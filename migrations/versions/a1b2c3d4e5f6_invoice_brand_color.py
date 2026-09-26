"""companies.invoice_brand_color for per-tenant PDF branding

MARSOUD-INVOICE-BRAND-COLOR (2026-09-26) — a single hex color that
re-tints every "brand" surface on the invoice PDF (title, table headers,
corner triangles, due-amount number, footer highlight). PAID (green) and
OVERDUE (red) stay semantic — those don't follow the brand. NULL means
"use the app default #059669", so companies that never touched the
setting render identically to before.

Revision ID: a1b2c3d4e5f6
Revises: f7a3c8b9d012
Create Date: 2026-09-26 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "f7a3c8b9d012"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("invoice_brand_color", sa.String(length=9),
                                    nullable=True))


def downgrade():
    with op.batch_alter_table("companies") as batch:
        batch.drop_column("invoice_brand_color")
