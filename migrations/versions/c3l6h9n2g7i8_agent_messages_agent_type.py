"""MARSOUD-INSIGHTS-AGENT-01 (Batch 9 Ticket 6, 2026-08-01).

Adds `agent_messages.agent_type` — String(20), default
'accountant', not-null (with backfill). Legacy rows all belong
to the accountant so 'accountant' is the safe backfill value.

Insights chats will land as `agent_type = 'insights'`. The
routes filter by agent_type when loading history so the two
panels never mix conversations.

Revision ID: c3l6h9n2g7i8
Revises: b2k5g8m1f6h7
Create Date: 2026-08-01
"""
from alembic import op
import sqlalchemy as sa


revision = 'c3l6h9n2g7i8'
down_revision = 'b2k5g8m1f6h7'
branch_labels = None
depends_on = None


def _has_col(insp, table, col):
    return any(c["name"] == col for c in insp.get_columns(table))


def upgrade():
    insp = sa.inspect(op.get_bind())
    if not _has_col(insp, "agent_messages", "agent_type"):
        with op.batch_alter_table("agent_messages") as batch:
            batch.add_column(sa.Column(
                "agent_type", sa.String(20), nullable=True))
        # Backfill legacy rows before enforcing NOT NULL.
        op.execute("UPDATE agent_messages SET agent_type = "
                   "'accountant' WHERE agent_type IS NULL")
        with op.batch_alter_table("agent_messages") as batch:
            batch.alter_column("agent_type", nullable=False,
                                server_default=sa.text(
                                    "'accountant'"))
        try:
            op.create_index("ix_agent_messages_agent_type",
                            "agent_messages", ["agent_type"])
        except Exception:
            pass


def downgrade():
    insp = sa.inspect(op.get_bind())
    if _has_col(insp, "agent_messages", "agent_type"):
        try:
            op.drop_index("ix_agent_messages_agent_type",
                          table_name="agent_messages")
        except Exception:
            pass
        with op.batch_alter_table("agent_messages") as batch:
            batch.drop_column("agent_type")
