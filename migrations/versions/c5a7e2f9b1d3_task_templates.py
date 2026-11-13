"""Tasks: task_templates (saved task templates per company)  # MARSOUD-TASKS-P61

Revision ID: c5a7e2f9b1d3
Revises: b4e8c1a7d9f2
Create Date: 2026-11-14 01:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = "c5a7e2f9b1d3"
down_revision = "b4e8c1a7d9f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_task_templates_company_id", "task_templates", ["company_id"])


def downgrade():
    op.drop_index("ix_task_templates_company_id", table_name="task_templates")
    op.drop_table("task_templates")
