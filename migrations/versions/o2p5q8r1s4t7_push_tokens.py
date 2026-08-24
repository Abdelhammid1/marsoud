"""MARSOUD-MOBILE-TKT-05 (2026-08-18) — push_tokens table.

Stores FCM registration tokens for mobile push notifications.
One row per (user, token) — enforced by a unique constraint so
the mobile client can idempotently POST its current token on
every login without producing duplicates.

Idempotent (`_has_table` guarded) — safe to re-run.

Revision ID: o2p5q8r1s4t7
Revises: 63492bd67619
Create Date: 2026-08-18
"""
from alembic import op
import sqlalchemy as sa


revision = "o2p5q8r1s4t7"
down_revision = "63492bd67619"
branch_labels = None
depends_on = None


def _has_table(name):
    try:
        return name in sa.inspect(op.get_bind()).get_table_names()
    except Exception:
        return False


def upgrade():
    if _has_table("push_tokens"):
        return
    op.create_table(
        "push_tokens",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer,
                   sa.ForeignKey("users.id", ondelete="CASCADE"),
                   nullable=False),
        sa.Column("token", sa.String(400), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False,
                   server_default="android"),
        sa.Column("device_label", sa.String(120), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False,
                   server_default=sa.text("1")),
        sa.Column("last_used_at", sa.DateTime, nullable=False,
                   server_default=sa.func.current_timestamp()),
        sa.Column("created_at", sa.DateTime, nullable=False,
                   server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("user_id", "token",
                             name="uq_push_tokens_user_token"),
    )
    with op.batch_alter_table("push_tokens") as batch:
        batch.create_index("ix_push_tokens_user_id", ["user_id"])
        batch.create_index("ix_push_tokens_token", ["token"])
        batch.create_index("ix_push_tokens_is_active", ["is_active"])


def downgrade():
    if not _has_table("push_tokens"):
        return
    with op.batch_alter_table("push_tokens") as batch:
        for ix in ("ix_push_tokens_is_active",
                    "ix_push_tokens_token",
                    "ix_push_tokens_user_id"):
            try:
                batch.drop_index(ix)
            except Exception:
                pass
    op.drop_table("push_tokens")
