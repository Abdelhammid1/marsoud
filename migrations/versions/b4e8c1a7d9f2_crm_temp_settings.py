"""CRM: crm_temp_settings (per-company lead temperature settings)  # MARSOUD-CRM-P25

Revision ID: b4e8c1a7d9f2
Revises: a9b3d5e7c2f4
Create Date: 2026-11-11 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "b4e8c1a7d9f2"
down_revision = "a9b3d5e7c2f4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "crm_temp_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_crm_temp_settings_company_id", "crm_temp_settings", ["company_id"], unique=True)


def downgrade():
    op.drop_index("ix_crm_temp_settings_company_id", table_name="crm_temp_settings")
    op.drop_table("crm_temp_settings")
