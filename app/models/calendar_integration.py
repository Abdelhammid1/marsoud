"""MARSOUD-P120 — Company-level Google Bookings & Calendar Sync settings."""
import secrets
from datetime import datetime
from app import db


class CalendarIntegration(db.Model):
    __tablename__ = "calendar_integrations"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, unique=True, index=True)
    google_ical_url = db.Column(db.String(1000), nullable=True)
    default_assignee_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                                    nullable=True)
    is_active = db.Column(db.Boolean, default=False, nullable=False)
    webhook_secret = db.Column(db.String(64), nullable=False,
                               default=lambda: secrets.token_urlsafe(24))
    feed_token = db.Column(db.String(64), nullable=False, unique=True, index=True,
                           default=lambda: secrets.token_urlsafe(24))
    last_synced_at = db.Column(db.DateTime, nullable=True)
    last_sync_status = db.Column(db.String(32), nullable=True)
    last_sync_error = db.Column(db.Text, nullable=True)
    synced_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    company = db.relationship("Company")
    default_assignee = db.relationship("User", foreign_keys=[default_assignee_id])

    @classmethod
    def get_or_create(cls, company_id: int) -> "CalendarIntegration":
        row = cls.query.filter_by(company_id=company_id).first()
        if row is None:
            row = cls(company_id=company_id)
            db.session.add(row)
            db.session.commit()
        return row
