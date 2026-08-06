"""MARSOUD-AGENT-MEMORY-05 (2026-08-06) — one conversation the user
holds with the agent (accountant or insights).

Scope key = (company_id, user_id, agent_type). The chat loader
filters strictly by conversation_id; nothing here loads across
conversations, so a two-month-old topic can't bleed into today's
question.

Life cycle:
  · created when the user starts a chat OR clicks "+ محادثة جديدة"
  · last_message_at updated on every user or assistant message
  · is_archived=True is the user-initiated "remove from sidebar"
    (soft-delete); the row + messages linger until the cron sweep
    hard-deletes conversations older than
    PlatformSetting.agent_conversation_retention_days.
"""
from datetime import datetime
from app import db


class AgentConversation(db.Model):
    __tablename__ = "agent_conversations"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    # Same axis AgentMessage uses — never mix accountant vs insights
    # in one conversation row.
    agent_type = db.Column(db.String(20), nullable=False,
                            default="accountant", index=True)
    title = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)
    last_message_at = db.Column(db.DateTime, default=datetime.utcnow,
                                 nullable=False, index=True)
    is_archived = db.Column(db.Boolean, default=False, nullable=False,
                             index=True)

    company = db.relationship("Company")
    user = db.relationship("User")
