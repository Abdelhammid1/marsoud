"""add shift_id to invoices

Revision ID: f6f7c2f44dd3
Revises: p4d1a8b6c5e7
Create Date: 2026-06-11 14:55:32.629904

"""
from alembic import op
import sqlalchemy as sa

revision = 'f6f7c2f44dd3'
down_revision = 'p4d1a8b6c5e7'
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('shift_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_invoices_shift_id', ['shift_id'], unique=False)
        batch_op.create_foreign_key('fk_invoices_shift_id', 'cashier_shifts', ['shift_id'], ['id'])

def downgrade():
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_constraint('fk_invoices_shift_id', type_='foreignkey')
        batch_op.drop_index('ix_invoices_shift_id')
        batch_op.drop_column('shift_id')
