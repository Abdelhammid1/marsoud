"""MARSOUD-P124 — Company-level Meta Lead Ads & Web Form Webhook settings."""
import secrets
from datetime import datetime
from app import db


class LeadIntegration(db.Model):
    __tablename__ = "lead_integrations"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, unique=True, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    webhook_secret = db.Column(db.String(64), nullable=False, unique=True, index=True,
                               default=lambda: secrets.token_urlsafe(24))
    verify_token = db.Column(db.String(64), nullable=False,
                             default=lambda: "marsoud_" + secrets.token_hex(12))
    meta_app_secret = db.Column(db.String(128), nullable=True)
    default_assignee_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                                    nullable=True)
    default_campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"),
                                    nullable=True)
    default_source = db.Column(db.String(100), default="Meta / Web Form", nullable=False)
    leads_received = db.Column(db.Integer, default=0, nullable=False)
    last_received_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    company = db.relationship("Company")
    default_assignee = db.relationship("User", foreign_keys=[default_assignee_id])
    default_campaign = db.relationship("Campaign", foreign_keys=[default_campaign_id])

    @classmethod
    def get_or_create(cls, company_id: int) -> "LeadIntegration":
        row = cls.query.filter_by(company_id=company_id).first()
        if row is None:
            row = cls(company_id=company_id)
            db.session.add(row)
            db.session.commit()
        return row
