"""P120: add calendar_integrations table for Google Bookings and Calendar sync

Revision ID: p120_calendar_integrations
Revises: p112_invoice_items_tracking
Create Date: 2026-11-23 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "p120_calendar_integrations"
down_revision = "p112_invoice_items_tracking"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "calendar_integrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("google_ical_url", sa.String(length=1000), nullable=True),
        sa.Column("default_assignee_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("webhook_secret", sa.String(length=64), nullable=False),
        sa.Column("feed_token", sa.String(length=64), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_sync_status", sa.String(length=32), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("synced_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_calendar_integrations_company_id", "calendar_integrations", ["company_id"], unique=True)
    op.create_index("ix_calendar_integrations_feed_token", "calendar_integrations", ["feed_token"], unique=True)


def downgrade():
    op.drop_index("ix_calendar_integrations_feed_token", table_name="calendar_integrations")
    op.drop_index("ix_calendar_integrations_company_id", table_name="calendar_integrations")
    op.drop_table("calendar_integrations")
