"""MARSOUD-CRM-EXPANSION — new lightweight models:

  Campaign      — marketing campaigns that produced a lead
  LeadContact   — one lead/customer can have several contact persons
  LeadActivity  — every touchpoint (call/email/meeting/note) + follow-up

Kept in their own module so the git-blame on crm.py stays sensible.
The FK linking Lead → Campaign lives on the existing Lead model (added
in a migration alongside the new tables).
"""
import enum
from datetime import datetime
from app import db


class Campaign(db.Model):
    __tablename__ = "campaigns"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    company = db.relationship("Company")
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        db.UniqueConstraint("company_id", "name", name="uq_campaign_company_name"),
    )

    def __repr__(self):
        return f"<Campaign #{self.id} {self.name}>"


class LeadContact(db.Model):
    """Extra contact persons attached to a lead or customer (the manager,
    the accountant, the tech lead, etc.). One party can have several."""
    __tablename__ = "lead_contacts"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"),
                         nullable=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"),
                             nullable=True, index=True)
    name = db.Column(db.String(150), nullable=False)
    role = db.Column(db.String(100))
    email = db.Column(db.String(200))
    phone = db.Column(db.String(30))
    is_primary = db.Column(db.Boolean, default=False, nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    company = db.relationship("Company")
    lead = db.relationship("Lead", foreign_keys=[lead_id])
    customer = db.relationship("Customer", foreign_keys=[customer_id])


class LeadActivityType(enum.Enum):
    CALL = "CALL"
    EMAIL = "EMAIL"
    MEETING = "MEETING"
    NOTE = "NOTE"

    @property
    def label_ar(self):
        return {
            "CALL":    "مكالمة",
            "EMAIL":   "إيميل",
            "MEETING": "اجتماع",
            "NOTE":    "ملاحظة",
        }[self.value]

    @property
    def icon(self):
        return {"CALL": "📞", "EMAIL": "✉️", "MEETING": "🤝", "NOTE": "📝"}[self.value]


class LeadActivity(db.Model):
    """One touchpoint with a lead — a call, email, meeting, or a plain
    note. Optionally schedules a follow-up date shown on the alerts
    strip."""
    __tablename__ = "lead_activities"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"),
                         nullable=False, index=True)
    type = db.Column(db.Enum(LeadActivityType), nullable=False, index=True)
    subject = db.Column(db.String(255))
    body = db.Column(db.Text)
    activity_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # Asmaa 2026-07-02 — was Date, now DateTime so activities can
    # carry an actual meeting time ("الثلاثاء 3 عصراً"), not just the
    # day. Migration d6_e3f9a2b7c8d handled the schema swap; legacy
    # date-only rows read cleanly as YYYY-MM-DD 00:00:00.
    follow_up_date = db.Column(db.DateTime, nullable=True, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                                nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    company = db.relationship("Company")
    lead = db.relationship("Lead", foreign_keys=[lead_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
