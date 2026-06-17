"""MARSOUD-57.2 — Plan + SubscriptionReminderSent models."""
import json
from datetime import datetime
from app import db


class Plan(db.Model):
    """Commercial plan a company subscribes to.

    `allowed_modules` is a JSON list of coarse-grained module codes such as
    "accounting", "sales", "inventory". `has_permission()` checks the
    action's module against this list before answering True.
    """
    __tablename__ = "plans"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), nullable=False, unique=True)
    name_ar = db.Column(db.String(120), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    price_monthly = db.Column(db.Numeric(10, 2))
    price_yearly = db.Column(db.Numeric(10, 2))
    allowed_modules = db.Column(db.Text, nullable=False, default="[]")
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def modules(self):
        try:
            data = json.loads(self.allowed_modules or "[]")
            return [m for m in data if isinstance(m, str)]
        except (ValueError, TypeError):
            return []

    def set_modules(self, modules):
        self.allowed_modules = json.dumps(list(modules))


class SubscriptionReminderSent(db.Model):
    """Tracks which subscription-expiry reminder thresholds have been sent
    for each company so the cron doesn't re-send. Mirrors
    InvoiceReminderSent."""
    __tablename__ = "subscription_reminders_sent"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    threshold_days = db.Column(db.Integer, nullable=False)
    expires_at_when_sent = db.Column(db.DateTime, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
