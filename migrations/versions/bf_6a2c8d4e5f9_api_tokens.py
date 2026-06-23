"""MARSOUD-API-V1 — api_tokens table

Bearer-token auth for the /api/v1/* JSON API. Each token belongs to a
single user, can be named + rotated independently. The raw token is
only ever shown to the user once at creation; we store SHA-256 in the
token_hash column. token_prefix is the first 12 visible chars for UI
display only ("mrs_live_abc12345…").

Revision ID: bf_6a2c8d4e5f9
Revises: be_5f1e9a4b7c3
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision = 'bf_6a2c8d4e5f9'
down_revision = 'be_5f1e9a4b7c3'
branch_labels = None
depends_on = None


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    if _has_table("api_tokens"):
        return
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False,
                  unique=True, index=True),
        sa.Column("token_prefix", sa.String(20), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, server_default="*"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )


def downgrade():
    if _has_table("api_tokens"):
        op.drop_table("api_tokens")
