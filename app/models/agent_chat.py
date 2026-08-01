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
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
