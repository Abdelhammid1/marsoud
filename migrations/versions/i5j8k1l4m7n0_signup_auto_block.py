"""MARSOUD-SIGNUP-AUTO-BLOCK (2026-08-12) — signup rejection
log + auto-learned domain blocklist.

Two new tables:

  A) signup_rejections — one row per rejected /register
     POST (honeypot / rate_limit / spam_domain / turnstile /
     blocked_domain). Cheap forensics + the fuel for
     auto-learning.

  B) blocked_domains — dynamically populated when the same
     email domain trips the honeypot ≥ 3 times in a 24-hour
     rolling window. Enforced by an is_domain_blocked()
     check that runs BEFORE the existing bot_guard gates.
     `is_active` + `unblocked_at` (soft-toggle, no delete)
     preserves the audit trail of every auto-block ever
     made.

Idempotent (_has_table guards), chains from g3h6i9j2k5l8.

Revision ID: i5j8k1l4m7n0
Revises: g3h6i9j2k5l8
Create Date: 2026-08-12
"""
from alembic import op
import sqlalchemy as sa


revision = "i5j8k1l4m7n0"
down_revision = "g3h6i9j2k5l8"
branch_labels = None
depends_on = None


TABLE_REJ = "signup_rejections"
TABLE_BLOCK = "blocked_domains"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def upgrade():
    if not _has_table(TABLE_REJ):
        op.create_table(
            TABLE_REJ,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("email_domain", sa.String(120),
                       nullable=False, index=True),
            sa.Column("reason", sa.String(24),
                       nullable=False, index=True),
            sa.Column("ip_address", sa.String(45), nullable=True),
            sa.Column("created_at", sa.DateTime,
                       server_default=sa.func.current_timestamp(),
                       nullable=False, index=True),
            sa.CheckConstraint(
                "reason IN ('honeypot','rate_limit',"
                "'spam_domain','turnstile','blocked_domain')",
                name="ck_signup_rejection_reason"),
        )
    if not _has_table(TABLE_BLOCK):
        op.create_table(
            TABLE_BLOCK,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("domain", sa.String(120),
                       nullable=False, unique=True, index=True),
            sa.Column("blocked_at", sa.DateTime,
                       server_default=sa.func.current_timestamp(),
                       nullable=False),
            sa.Column("reason", sa.String(200), nullable=True),
            sa.Column("is_active", sa.Boolean, nullable=False,
                       server_default="1", index=True),
            sa.Column("unblocked_at", sa.DateTime, nullable=True),
            sa.Column("unblocked_by_id", sa.Integer,
                       sa.ForeignKey("users.id",
                                      name="fk_blocked_domain_unblocker"),
                       nullable=True),
        )


def downgrade():
    if _has_table(TABLE_BLOCK):
        op.drop_table(TABLE_BLOCK)
    if _has_table(TABLE_REJ):
        op.drop_table(TABLE_REJ)
