"""MARSOUD-NEW-DEVICE (Abdelhamid 2026-07-22).

Adds user_sessions.device_signature VARCHAR(64) NULL — SHA-256[:32]
of (User-Agent || ip_class) used by start_session() to decide
whether we've seen this shape of client before, and skip the
"new device" alert email accordingly. Deliberately separate from
session_token (which stays unique + random per session).

Revision ID: g1b4d7f2a5c8
Revises: f0a3c6e9b4d2
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'g1b4d7f2a5c8'
down_revision = 'f0a3c6e9b4d2'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("user_sessions", "device_signature"):
        with op.batch_alter_table("user_sessions", schema=None) as batch:
            batch.add_column(sa.Column(
                "device_signature", sa.String(64), nullable=True))
            batch.create_index(
                "ix_user_sessions_device_sig",
                ["user_id", "device_signature"])


def downgrade():
    if _has_col("user_sessions", "device_signature"):
        with op.batch_alter_table("user_sessions", schema=None) as batch:
            try:
                batch.drop_index("ix_user_sessions_device_sig")
            except Exception:
                pass
            batch.drop_column("device_signature")
