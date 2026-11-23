"""P125: add whatsapp_integrations table for Zero-Ban WhatsApp & Meta Cloud API

Revision ID: p125_whatsapp_integrations
Revises: p124_lead_integrations
Create Date: 2026-11-23 16:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "p125_whatsapp_integrations"
down_revision = "p124_lead_integrations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "whatsapp_integrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("default_country_code", sa.String(length=8), nullable=False, server_default="966"),
        sa.Column("is_cloud_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("phone_number_id", sa.String(length=64), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("booking_template_name", sa.String(length=120), nullable=False, server_default="booking_confirmation"),
        sa.Column("booking_template_lang", sa.String(length=16), nullable=False, server_default="ar"),
        sa.Column("invoice_footer_note", sa.String(length=300), nullable=True),
        sa.Column("messages_sent", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_whatsapp_integrations_company_id", "whatsapp_integrations", ["company_id"], unique=True)


def downgrade():
    op.drop_index("ix_whatsapp_integrations_company_id", table_name="whatsapp_integrations")
    op.drop_table("whatsapp_integrations")
