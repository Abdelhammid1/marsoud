"""MARSOUD-ACTLOG-01 — user sessions + activity log.

  user_sessions       one row per login (token, login/logout/last_seen,
                      device metadata, status)
  user_activity_log   one row per significant action — VIEW / LOGIN /
                      LOGOUT / CREATE / UPDATE / DELETE / EXPORT etc.

Both tables index (company_id, user_id, created_at) for the activity
pages' default sort + filter.

Revision ID: c2_9f5b3e7d2a8
Revises: c1_8e4a2f6b9c1
Create Date: 2026-06-28
"""
from alembic import op
import sqlalchemy as sa

revision = 'c2_9f5b3e7d2a8'
down_revision = 'c1_8e4a2f6b9c1'
branch_labels = None
depends_on = None


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    if not _has_table("user_sessions"):
        op.create_table(
            "user_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=True, index=True),
            sa.Column("session_token", sa.String(80), nullable=False,
                      unique=True, index=True),
            sa.Column("login_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.current_timestamp()),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.current_timestamp()),
            sa.Column("logout_at", sa.DateTime(), nullable=True),
            sa.Column("ip_address", sa.String(45), nullable=True),
            sa.Column("user_agent", sa.String(500), nullable=True),
            sa.Column("device_type", sa.String(20), nullable=True),
            sa.Column("device_os", sa.String(40), nullable=True),
            sa.Column("browser", sa.String(40), nullable=True),
            sa.Column("status", sa.String(15), nullable=False,
                      server_default="ACTIVE"),
        )
        op.create_index("ix_us_company_user_login",
                        "user_sessions",
                        ["company_id", "user_id", "login_at"])

    if not _has_table("user_activity_log"):
        op.create_table(
            "user_activity_log",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=True, index=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id"),
                      nullable=True, index=True),
            sa.Column("session_id", sa.Integer(),
                      sa.ForeignKey("user_sessions.id", ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("action_type", sa.String(20), nullable=False,
                      index=True),
            sa.Column("entity_type", sa.String(40), nullable=True,
                      index=True),
            sa.Column("entity_id", sa.Integer(), nullable=True),
            sa.Column("entity_label", sa.String(255), nullable=True),
            sa.Column("route", sa.String(255), nullable=True),
            sa.Column("method", sa.String(10), nullable=True),
            sa.Column("ip_address", sa.String(45), nullable=True),
            sa.Column("device_type", sa.String(20), nullable=True),
            sa.Column("device_os", sa.String(40), nullable=True),
            sa.Column("browser", sa.String(40), nullable=True),
            sa.Column("extra_data", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.current_timestamp(),
                      index=True),
        )
        op.create_index("ix_ual_company_user_created",
                        "user_activity_log",
                        ["company_id", "user_id", "created_at"])


def downgrade():
    if _has_table("user_activity_log"):
        op.drop_table("user_activity_log")
    if _has_table("user_sessions"):
        op.drop_table("user_sessions")
