"""MARSOUD-AGENT-SAFETY-03 (2026-08-06) — safety layer above the
accountant agent's write operations.

Three additions, one migration:

  1. agent_messages.tool_trace — JSON blob storing every tool call
     the agent made for a given assistant message (name, inputs,
     result). Previously the chat log stored only the final text; a
     recent company-37 incident had no forensic trail for what the
     agent actually did.

  2. agent_proposals — new table. Every WRITE tool call (create
     customer / journal / invoice / payment) writes a PENDING proposal
     instead of executing immediately. The user clicks a button to
     confirm; only then does the tool run for real. Reads stay
     instant.

  3. agent_daily_write_counts — one row per (user, day) so a
     PlatformSetting-backed cap can refuse the Nth+1 write per day.
     Day boundaries are company-tz (Riyadh by default) not server-UTC.

Additive on agent_messages (single nullable text column), so old
messages read as NULL for the trace column — accepted.

Revision ID: u3r8c5f9x1d6
Revises: t2q7b4e8w0c5
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa


revision = 'u3r8c5f9x1d6'
down_revision = 't2q7b4e8w0c5'
branch_labels = None
depends_on = None


def _cols(table):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    bind = op.get_bind()

    # 1. agent_messages.tool_trace
    if _has_table("agent_messages") and "tool_trace" not in _cols("agent_messages"):
        op.add_column("agent_messages",
                      sa.Column("tool_trace", sa.Text, nullable=True))

    # 2. agent_proposals — one row per pending write. status is a
    # plain string enum ('PENDING'|'EXECUTED'|'CANCELLED'|'EXPIRED')
    # to sidestep the SQLite-Enum-CHECK trap the attendance tables
    # taught us to avoid.
    if not _has_table("agent_proposals"):
        op.create_table(
            "agent_proposals",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer,
                      sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("tool_name", sa.String(60), nullable=False),
            sa.Column("input_json", sa.Text, nullable=False),
            sa.Column("summary_ar", sa.Text),
            sa.Column("amount_readable", sa.String(80)),
            sa.Column("status", sa.String(20), nullable=False,
                      server_default="PENDING", index=True),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False,
                      index=True),
            sa.Column("executed_at", sa.DateTime),
            sa.Column("result_json", sa.Text),
        )

    # 3. agent_daily_write_counts
    if not _has_table("agent_daily_write_counts"):
        op.create_table(
            "agent_daily_write_counts",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("user_id", sa.Integer,
                      sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("day", sa.Date, nullable=False, index=True),
            sa.Column("count", sa.Integer, nullable=False,
                      server_default="0"),
            sa.UniqueConstraint("user_id", "day",
                                name="uq_agent_daily_write_user_day"),
        )


def downgrade():
    if _has_table("agent_daily_write_counts"):
        op.drop_table("agent_daily_write_counts")
    if _has_table("agent_proposals"):
        op.drop_table("agent_proposals")
    if "tool_trace" in _cols("agent_messages"):
        try:
            op.drop_column("agent_messages", "tool_trace")
        except Exception:
            pass
