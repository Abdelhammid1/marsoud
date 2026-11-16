"""P102 product expiry/serial tracking, stock lot expiry, and product_serials table

Revision ID: p102_product_tracking_serials
Revises: p101_company_industry
Create Date: 2026-11-16 07:05:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'p102_product_tracking_serials'
down_revision = 'p101_company_industry'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('track_expiry', sa.Boolean(), server_default=sa.text('false'), nullable=False))
        batch_op.add_column(sa.Column('track_serial', sa.Boolean(), server_default=sa.text('false'), nullable=False))
        batch_op.add_column(sa.Column('warranty_months', sa.Integer(), server_default='0', nullable=False))

    with op.batch_alter_table('stock_lots', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lot_number', sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column('expiry_date', sa.Date(), nullable=True))
        batch_op.create_index('ix_stock_lots_lot_number', ['lot_number'], unique=False)
        batch_op.create_index('ix_stock_lots_expiry_date', ['expiry_date'], unique=False)

    op.create_table(
        'product_serials',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('variant_id', sa.Integer(), nullable=False),
        sa.Column('warehouse_id', sa.Integer(), nullable=True),
        sa.Column('serial_number', sa.String(length=100), nullable=False),
        sa.Column('imei2', sa.String(length=100), nullable=True),
        sa.Column('status', sa.String(length=20), server_default='IN_STOCK', nullable=False),
        sa.Column('purchase_bill_id', sa.Integer(), nullable=True),
        sa.Column('sale_invoice_id', sa.Integer(), nullable=True),
        sa.Column('warranty_ends_at', sa.Date(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
        sa.ForeignKeyConstraint(['variant_id'], ['product_variants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['warehouse_id'], ['warehouses.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('company_id', 'serial_number', name='uq_product_serial_company_sn'),
    )
    op.create_index('ix_product_serials_company_id', 'product_serials', ['company_id'], unique=False)
    op.create_index('ix_product_serials_variant_id', 'product_serials', ['variant_id'], unique=False)
    op.create_index('ix_product_serials_warehouse_id', 'product_serials', ['warehouse_id'], unique=False)
    op.create_index('ix_product_serials_status', 'product_serials', ['status'], unique=False)


def downgrade():
    op.drop_table('product_serials')
    with op.batch_alter_table('stock_lots', schema=None) as batch_op:
        batch_op.drop_index('ix_stock_lots_expiry_date')
        batch_op.drop_index('ix_stock_lots_lot_number')
        batch_op.drop_column('expiry_date')
        batch_op.drop_column('lot_number')
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('warranty_months')
        batch_op.drop_column('track_serial')
        batch_op.drop_column('track_expiry')
