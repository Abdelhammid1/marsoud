"""add_lot_expiry_serial_to_vendor_bill_items

Revision ID: d1efd5035455
Revises: p102_product_tracking_serials
Create Date: 2026-11-17 01:31:48.206012

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1efd5035455'
down_revision = 'p102_product_tracking_serials'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE vendor_bill_items ADD COLUMN IF NOT EXISTS lot_number VARCHAR(80)")
    op.execute("ALTER TABLE vendor_bill_items ADD COLUMN IF NOT EXISTS expiry_date DATE")
    op.execute("ALTER TABLE vendor_bill_items ADD COLUMN IF NOT EXISTS serial_numbers_text TEXT")


def downgrade():
    op.execute("ALTER TABLE vendor_bill_items DROP COLUMN IF EXISTS serial_numbers_text")
    op.execute("ALTER TABLE vendor_bill_items DROP COLUMN IF EXISTS expiry_date")
    op.execute("ALTER TABLE vendor_bill_items DROP COLUMN IF EXISTS lot_number")
