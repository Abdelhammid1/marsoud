"""P126: add invoice_integrations table for External Platforms Invoice Webhook

Revision ID: p126_invoice_integrations
Revises: p125_whatsapp_integrations
Create Date: 2026-11-23 17:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "p126_invoice_integrations"
down_revision = "p125_whatsapp_integrations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "invoice_integrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("webhook_secret", sa.String(length=64), nullable=False),
        sa.Column("api_token", sa.String(length=64), nullable=False),
        sa.Column("default_payment_method_id", sa.Integer(), sa.ForeignKey("payment_methods.id"), nullable=True),
        sa.Column("default_tax_rate", sa.Numeric(5, 2), nullable=False, server_default=sa.text("15.00")),
        sa.Column("auto_post_ledger", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("invoices_received", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_received_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_invoice_integrations_company_id", "invoice_integrations", ["company_id"], unique=True)
    op.create_index("ix_invoice_integrations_webhook_secret", "invoice_integrations", ["webhook_secret"], unique=True)


def downgrade():
    op.drop_index("ix_invoice_integrations_webhook_secret", table_name="invoice_integrations")
    op.drop_index("ix_invoice_integrations_company_id", table_name="invoice_integrations")
    op.drop_table("invoice_integrations")
