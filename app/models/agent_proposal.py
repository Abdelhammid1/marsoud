"""MARSOUD-AGENT-SAFETY-03 (2026-08-06) — proposals awaiting user
confirmation, and daily write counters.

Both tables sit above the agent tools. The proposal is what the
user sees in the chat as a confirm/cancel card; the daily counter
is a per-user cap on how many writes the agent can execute in a
day for that user (day in company-tz)."""
from datetime import datetime
from app import db


PROPOSAL_PENDING = "PENDING"
PROPOSAL_EXECUTED = "EXECUTED"
PROPOSAL_CANCELLED = "CANCELLED"
PROPOSAL_EXPIRED = "EXPIRED"
PROPOSAL_STATUSES = (
    PROPOSAL_PENDING, PROPOSAL_EXECUTED,
    PROPOSAL_CANCELLED, PROPOSAL_EXPIRED,
)


class AgentProposal(db.Model):
    """One row per WRITE tool call the accountant agent wanted to run.

    Written in PENDING by execute_tool when confirmation is required.
    The chat UI renders the row as a confirm/cancel card. Confirm
    flips it to EXECUTED and runs the tool for real; cancel flips it
    to CANCELLED. Anything older than 24h is EXPIRED and refuses to
    run — a stale chat window cannot commit yesterday's misunderstanding
    to the ledger.
    """
    __tablename__ = "agent_proposals"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    tool_name = db.Column(db.String(60), nullable=False)
    input_json = db.Column(db.Text, nullable=False)
    summary_ar = db.Column(db.Text)
    amount_readable = db.Column(db.String(80))
    status = db.Column(db.String(20), nullable=False,
                       default=PROPOSAL_PENDING, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)
    executed_at = db.Column(db.DateTime)
    result_json = db.Column(db.Text)

    company = db.relationship("Company")
    user = db.relationship("User")


class AgentDailyWriteCount(db.Model):
    """Per-user, per-day counter of agent write operations.

    `day` is a date in the company's timezone (Riyadh by default),
    resolved by the caller via today_in_company_tz. Unique constraint
    on (user_id, day) so an upsert is one lookup + increment."""
    __tablename__ = "agent_daily_write_counts"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    day = db.Column(db.Date, nullable=False, index=True)
    count = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.UniqueConstraint("user_id", "day",
                            name="uq_agent_daily_write_user_day"),
    )
