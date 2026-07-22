"""MARSOUD-API-RATE-LIMIT (Abdelhamid 2026-07-22).

Adds api_token_windows (token_id, window_start_utc, count) composite
PK. The rate-limiter service keeps an in-memory dict of the CURRENT
window per token for O(1) checks + writes through to this table so
counters survive process restarts and stay consistent across a
handful of gunicorn workers.

Revision ID: h2c5e8a1b4d7
Revises: g1b4d7f2a5c8
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'h2c5e8a1b4d7'
down_revision = 'g1b4d7f2a5c8'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "api_token_windows" not in insp.get_table_names():
        op.create_table(
            "api_token_windows",
            sa.Column("token_id", sa.Integer(),
                      sa.ForeignKey("api_tokens.id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("window_start_utc", sa.DateTime(),
                      primary_key=True),
            sa.Column("count", sa.Integer(),
                      nullable=False, server_default="0"),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "api_token_windows" in insp.get_table_names():
        op.drop_table("api_token_windows")
