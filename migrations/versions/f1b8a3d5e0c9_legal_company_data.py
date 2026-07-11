"""MARSOUD-COMPANY-LEGAL — legal_name + brand_name + commercial_register_no.

Ticket: no place in the UI to record the company's legal name, brand
name, or commercial-registration number. Abdelhamid wants these on
every invoice / quotation / receipt / email / PDF the system emits.

Adds three nullable text columns to the companies table so existing
tenants can keep their current single `name` field until they choose
to fill in the new ones. Templates fall back gracefully to `name`
when the new columns are empty.

Revision ID: f1b8a3d5e0c9
Revises: e0a7b4c6f2d8
"""
from alembic import op
import sqlalchemy as sa


revision = "f1b8a3d5e0c9"
down_revision = "e0a7b4c6f2d8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("companies") as batch_op:
        batch_op.add_column(sa.Column(
            "legal_name", sa.String(200), nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "brand_name", sa.String(150), nullable=True,
        ))
        batch_op.add_column(sa.Column(
            "commercial_register_no", sa.String(50), nullable=True,
        ))


def downgrade():
    with op.batch_alter_table("companies") as batch_op:
        batch_op.drop_column("commercial_register_no")
        batch_op.drop_column("brand_name")
        batch_op.drop_column("legal_name")
