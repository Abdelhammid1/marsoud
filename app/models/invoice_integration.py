"""MARSOUD-P126 — Company-level External Platforms (Schools / Stores) Invoice Webhook settings."""
import secrets
from datetime import datetime
from app import db


class InvoiceIntegration(db.Model):
    __tablename__ = "invoice_integrations"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, unique=True, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    webhook_secret = db.Column(db.String(64), nullable=False, unique=True, index=True,
                               default=lambda: secrets.token_urlsafe(24))
    api_token = db.Column(db.String(64), nullable=False,
                          default=lambda: "inv_" + secrets.token_hex(12))
    default_payment_method_id = db.Column(db.Integer, db.ForeignKey("payment_methods.id"),
                                          nullable=True)
    default_tax_rate = db.Column(db.Numeric(5, 2), default=15.00, nullable=False)
    auto_post_ledger = db.Column(db.Boolean, default=True, nullable=False)
    invoices_received = db.Column(db.Integer, default=0, nullable=False)
    last_received_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    company = db.relationship("Company")
    default_payment_method = db.relationship("PaymentMethod", foreign_keys=[default_payment_method_id])

    @classmethod
    def get_or_create(cls, company_id: int) -> "InvoiceIntegration":
        row = cls.query.filter_by(company_id=company_id).first()
        if row is None:
            row = cls(company_id=company_id)
            db.session.add(row)
            db.session.commit()
        return row
