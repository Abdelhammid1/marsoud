"""MARSOUD-QUOTAS (Abdelhamid 2026-07-22) — model layer.

The service layer at app/services/quotas.py wraps all mutations.
"""
from datetime import datetime
from app import db


# Quota type constants — kept as strings for extensibility.
QUOTA_USERS = "users"
QUOTA_AI_TOKENS_MONTH = "ai_tokens_month"
QUOTA_STORAGE_BYTES = "storage_bytes"
QUOTA_BRANCHES = "branches"
KNOWN_QUOTA_TYPES = (QUOTA_USERS, QUOTA_AI_TOKENS_MONTH,
                     QUOTA_STORAGE_BYTES, QUOTA_BRANCHES)

# Enforcement modes.
ENF_BLOCK = "BLOCK"
ENF_ALLOW_NOTIFY = "ALLOW_NOTIFY"
ENF_UNLIMITED = "UNLIMITED"
ENFORCEMENT_MODES = (ENF_BLOCK, ENF_ALLOW_NOTIFY, ENF_UNLIMITED)


class Quota(db.Model):
    __tablename__ = "quotas"

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer,
                        db.ForeignKey("plans.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    quota_type = db.Column(db.String(40), nullable=False)
    included_amount = db.Column(db.BigInteger,
                                default=0, nullable=False)
    enforcement_mode = db.Column(db.String(20),
                                  default=ENF_UNLIMITED,
                                  nullable=False)
    price_per_extra_unit = db.Column(db.Numeric(15, 4), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("plan_id", "quota_type",
                             name="uq_quotas_plan_type"),
    )


class AiTokenUsage(db.Model):
    __tablename__ = "ai_token_usage"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=True)
    provider = db.Column(db.String(20), default="anthropic",
                         nullable=False)
    model = db.Column(db.String(80))
    input_tokens = db.Column(db.Integer, default=0, nullable=False)
    output_tokens = db.Column(db.Integer, default=0, nullable=False)
    total_tokens = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)


class EmployeeAiCap(db.Model):
    """Per-employee monthly ceiling — subset of the company's overall
    AI tokens quota. When set + reached, the employee is blocked
    even if the company still has budget."""
    __tablename__ = "employee_ai_caps"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False)
    monthly_cap = db.Column(db.BigInteger, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("company_id", "user_id",
                             name="uq_employee_ai_cap"),
    )


class QuotaNotificationSent(db.Model):
    """Dedupe rail for the 80/90/100% owner notifications. cycle_month
    is a "YYYY-MM" string so a fresh calendar month resets the
    dedupe naturally."""
    __tablename__ = "quota_notifications_sent"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    quota_type = db.Column(db.String(40), nullable=False)
    threshold_pct = db.Column(db.Integer, nullable=False)
    cycle_month = db.Column(db.String(7), nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow,
                        nullable=False)

    __table_args__ = (
        db.UniqueConstraint(
            "company_id", "quota_type", "threshold_pct", "cycle_month",
            name="uq_quota_notif"),
    )
