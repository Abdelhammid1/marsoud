"""MARSOUD-CONSENT-AUDIT-LOG (Abdelhamid 2026-07-22).

Append-only history of every legal-consent event. Never updated,
never deleted (soft-freezing at the model level would be a next
step; for now we rely on the app being the only writer).
"""
from datetime import datetime
from app import db


class ConsentEvent(db.Model):
    __tablename__ = "consent_events"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    # Nullable — backfilled rows from before Ticket B shipped don't
    # know which active_company was up at the time.
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id"),
                           nullable=True, index=True)
    consent_type = db.Column(db.String(30), nullable=False)
    document_version = db.Column(db.String(20), nullable=False)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(500))
    source = db.Column(db.String(30), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)

    user = db.relationship("User", foreign_keys=[user_id])
    company = db.relationship("Company", foreign_keys=[company_id])
