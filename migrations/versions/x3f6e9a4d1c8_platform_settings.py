"""platform_settings key/value table

Adds a single table for super-admin-tunable platform-wide settings
(subscription reminder thresholds, grace days, read-only toggle).
Defaults live in app/services/subscription.py so the system works
on day 0 without seeding any rows.

Revision ID: x3f6e9a4d1c8
Revises: w2e8f5a9c3b7
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa

revision = 'x3f6e9a4d1c8'
down_revision = 'w2e8f5a9c3b7'
branch_labels = None
depends_on = None


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    if not _has_table("platform_settings"):
        op.create_table(
            "platform_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("key", sa.String(60), nullable=False, unique=True),
            sa.Column("value", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True,
                      server_default=sa.func.current_timestamp()),
        )


def downgrade():
    if _has_table("platform_settings"):
        op.drop_table("platform_settings")
