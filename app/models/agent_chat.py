from datetime import datetime
from app import db


class AgentMessage(db.Model):
    __tablename__ = "agent_messages"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    # MARSOUD-INSIGHTS-AGENT-01 (Batch 9 Ticket 6, 2026-08-01)
    # — distinguishes accountant chats from insights chats so
    # the two panels never mix histories. Legacy rows all
    # backfilled to "accountant" by the migration.
    agent_type = db.Column(db.String(20), nullable=False,
                            default="accountant", index=True)
    # MARSOUD-AGENT-SAFETY-03 (2026-08-06) — JSON list of tool calls
    # this assistant message emitted. Each entry:
    #   {"tool": name, "input": {...}, "result": {...}}
    # Null on user messages and on legacy assistant messages
    # written before this ticket. The chat log used to store only
    # the assistant's final text, which meant company-37's mystery
    # was unreproducible: "what did the agent actually do?" had no
    # answer. It does now.
    tool_trace = db.Column(db.Text, nullable=True)
    # MARSOUD-AGENT-MEMORY-05 (2026-08-06) — the conversation this
    # message belongs to. Nullable in the schema so the migration
    # can add the column before the backfill runs; the backfill
    # then fills every legacy row with its (company, user,
    # agent_type)-bucketed AgentConversation. Every new message
    # written after this ticket has a non-null value.
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("agent_conversations.id"),
        nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
