"""MARSOUD-AGENT-MEMORY-05 (2026-08-06) — conversation boundaries
for the agent chat.

Before this migration, AgentMessage rows had no notion of "which
conversation". _load_history loaded the last 20 rows per (user,
company, agent_type) with no time bound — so a message from two
months ago was silently included in every new turn's context, and
the user had no way to start a clean slate.

Two changes:

  1. agent_conversations — new table. One row per conversation the
     user has with a specific agent (accountant | insights). Rows
     stay around when archived; a cron sweep hard-deletes after
     N days per PlatformSetting agent_conversation_retention_days.

  2. agent_messages.conversation_id — nullable FK. Legacy rows get
     backfilled inline: one AgentConversation per distinct
     (company_id, user_id, agent_type) tuple in the existing
     messages, titled "محادثة قديمة" + earliest message date.
     Every legacy message updates to point at its bucket, so an
     existing user still sees their prior chats in the sidebar
     instead of losing them.

Revision ID: v4t9d6g0y2e7
Revises: u3r8c5f9x1d6
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa


revision = 'v4t9d6g0y2e7'
down_revision = 'u3r8c5f9x1d6'
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

    # 1. agent_conversations
    if not _has_table("agent_conversations"):
        op.create_table(
            "agent_conversations",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer,
                      sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("agent_type", sa.String(20), nullable=False,
                      server_default="accountant", index=True),
            sa.Column("title", sa.String(200)),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False),
            sa.Column("last_message_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False,
                      index=True),
            sa.Column("is_archived", sa.Boolean, nullable=False,
                      server_default=sa.false(), index=True),
        )

    # 2. agent_messages.conversation_id
    if _has_table("agent_messages") and "conversation_id" not in _cols("agent_messages"):
        op.add_column("agent_messages",
                      sa.Column("conversation_id", sa.Integer,
                                nullable=True, index=True))

    # 3. LEGACY BACKFILL — one conversation per (company, user,
    # agent_type) tuple in the existing messages, then point every
    # NULL conversation_id message at its bucket. Skipped if there
    # are no legacy rows.
    tuples = bind.execute(sa.text(
        "SELECT DISTINCT company_id, user_id, agent_type "
        "FROM agent_messages WHERE conversation_id IS NULL"
    )).fetchall()

    for (cid, uid, atype) in tuples:
        # Earliest message date for the bucket's title.
        earliest = bind.execute(sa.text(
            "SELECT MIN(created_at) FROM agent_messages "
            "WHERE company_id=:c AND user_id=:u "
            "AND agent_type=:t AND conversation_id IS NULL"
        ), {"c": cid, "u": uid, "t": atype}).scalar()
        title = "محادثة قديمة"
        if earliest is not None:
            title = f"محادثة قديمة — {str(earliest)[:10]}"
        conv = bind.execute(sa.text(
            "INSERT INTO agent_conversations "
            "(company_id, user_id, agent_type, title, "
            " created_at, last_message_at, is_archived) "
            "VALUES (:c, :u, :t, :ti, :ca, :ca, 0) "
            "RETURNING id" if bind.dialect.name == "postgresql"
            else "INSERT INTO agent_conversations "
                 "(company_id, user_id, agent_type, title, "
                 " created_at, last_message_at, is_archived) "
                 "VALUES (:c, :u, :t, :ti, :ca, :ca, 0)"
        ), {"c": cid, "u": uid, "t": atype or "accountant",
            "ti": title, "ca": earliest or sa.func.now()})
        if bind.dialect.name == "postgresql":
            new_id = conv.scalar()
        else:
            new_id = bind.execute(sa.text(
                "SELECT last_insert_rowid()")).scalar()
        bind.execute(sa.text(
            "UPDATE agent_messages SET conversation_id=:cv "
            "WHERE company_id=:c AND user_id=:u "
            "AND agent_type=:t AND conversation_id IS NULL"
        ), {"cv": new_id, "c": cid, "u": uid,
            "t": atype})


def downgrade():
    if "conversation_id" in _cols("agent_messages"):
        try:
            op.drop_column("agent_messages", "conversation_id")
        except Exception:
            pass
    if _has_table("agent_conversations"):
        op.drop_table("agent_conversations")
