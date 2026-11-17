"""P112: add lot_number, expiry_date, serial_numbers_text to invoice_items

Revision ID: p112_invoice_items_tracking
Revises: d1efd5035455
"""
from alembic import op
import sqlalchemy as sa

revision = 'p112_invoice_items_tracking'
down_revision = 'd1efd5035455'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('invoice_items', sa.Column('lot_number', sa.String(length=80), nullable=True))
    op.add_column('invoice_items', sa.Column('expiry_date', sa.Date(), nullable=True))
    op.add_column('invoice_items', sa.Column('serial_numbers_text', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('invoice_items', 'serial_numbers_text')
    op.drop_column('invoice_items', 'expiry_date')
    op.drop_column('invoice_items', 'lot_number')
