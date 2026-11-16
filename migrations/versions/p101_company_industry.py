"""P101 add industry_code and industry_features to companies

Revision ID: p101_company_industry
Revises: e9d4a6b2c8f1
Create Date: 2026-11-16 06:45:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'p101_company_industry'
down_revision = 'e9d4a6b2c8f1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('companies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('industry_code', sa.String(length=32), server_default='general', nullable=False))
        batch_op.add_column(sa.Column('industry_features', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('companies', schema=None) as batch_op:
        batch_op.drop_column('industry_features')
        batch_op.drop_column('industry_code')
