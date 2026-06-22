"""MARSOUD — lead_comments table (ticket C / image #47).

  Mirrors task_comments shape so the UX is consistent: a Lead can have
  a thread of dated comments by company members, each with optional
  attachment metadata.

Revision ID: be_5f1e9a4b7c3
Revises: bd_4e9f1c6d8a3
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa

revision = 'be_5f1e9a4b7c3'
down_revision = 'bd_4e9f1c6d8a3'
branch_labels = None
depends_on = None


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    if _has_table("lead_comments"):
        return
    op.create_table(
        "lead_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.Integer(),
                  sa.ForeignKey("leads.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("company_id", sa.Integer(),
                  sa.ForeignKey("companies.id"),
                  nullable=False, index=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("attachment_url", sa.String(400), nullable=True),
        sa.Column("attachment_name", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.current_timestamp()),
    )


def downgrade():
    if _has_table("lead_comments"):
        op.drop_table("lead_comments")
