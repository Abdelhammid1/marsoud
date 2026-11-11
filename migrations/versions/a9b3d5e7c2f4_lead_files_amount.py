"""CRM: amount on lead_quotation_meta (now used for every lead file kind)  # MARSOUD-CRM-P19

Revision ID: a9b3d5e7c2f4
Revises: f7a2c9d4b1e6
Create Date: 2026-11-11 08:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "a9b3d5e7c2f4"
down_revision = "f7a2c9d4b1e6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("lead_quotation_meta", sa.Column("amount", sa.Numeric(14, 2), nullable=True))


def downgrade():
    with op.batch_alter_table("lead_quotation_meta") as batch:
        batch.drop_column("amount")
