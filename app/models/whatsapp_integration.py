"""MARSOUD-P125 — Company-level Zero-Ban WhatsApp & Official Meta Cloud API settings."""
from datetime import datetime
from app import db


class WhatsAppIntegration(db.Model):
    __tablename__ = "whatsapp_integrations"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, unique=True, index=True)
    default_country_code = db.Column(db.String(8), default="966", nullable=False)
    is_cloud_enabled = db.Column(db.Boolean, default=False, nullable=False)
    phone_number_id = db.Column(db.String(64), nullable=True)
    access_token = db.Column(db.Text, nullable=True)
    booking_template_name = db.Column(db.String(120), default="booking_confirmation", nullable=False)
    booking_template_lang = db.Column(db.String(16), default="ar", nullable=False)
    invoice_footer_note = db.Column(db.String(300), nullable=True)
    messages_sent = db.Column(db.Integer, default=0, nullable=False)
    last_sent_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    company = db.relationship("Company")

    @classmethod
    def get_or_create(cls, company_id: int) -> "WhatsAppIntegration":
        row = cls.query.filter_by(company_id=company_id).first()
        if row is None:
            row = cls(company_id=company_id)
            db.session.add(row)
            db.session.commit()
        return row
