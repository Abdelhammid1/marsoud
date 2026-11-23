"""P124: add lead_integrations table for Meta Lead Ads and Web Form webhooks

Revision ID: p124_lead_integrations
Revises: p120_calendar_integrations
Create Date: 2026-11-23 15:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "p124_lead_integrations"
down_revision = "p120_calendar_integrations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "lead_integrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("webhook_secret", sa.String(length=64), nullable=False),
        sa.Column("verify_token", sa.String(length=64), nullable=False),
        sa.Column("meta_app_secret", sa.String(length=128), nullable=True),
        sa.Column("default_assignee_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("default_campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id"), nullable=True),
        sa.Column("default_source", sa.String(length=100), nullable=False, server_default="Meta / Web Form"),
        sa.Column("leads_received", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_received_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_lead_integrations_company_id", "lead_integrations", ["company_id"], unique=True)
    op.create_index("ix_lead_integrations_webhook_secret", "lead_integrations", ["webhook_secret"], unique=True)


def downgrade():
    op.drop_index("ix_lead_integrations_webhook_secret", table_name="lead_integrations")
    op.drop_index("ix_lead_integrations_company_id", table_name="lead_integrations")
    op.drop_table("lead_integrations")
