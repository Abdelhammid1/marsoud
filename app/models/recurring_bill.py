"""Recurring vendor bill template + per-occurrence overrides.

This module is a PROJECTION layer only — it never creates journal
entries. The user posts the actual vendor bill when it arrives; this
just tells them what's coming and when.
"""
from datetime import datetime
from app import db


# Intervals supported by the forecast expansion. Stored as the literal
# string so a Python enum collision doesn't cause grief on upgrade.
INTERVAL_UNITS = ("DAY", "WEEK", "MONTH", "YEAR")
OVERRIDE_ACTIONS = ("SKIP", "AMEND")


class RecurringBill(db.Model):
    __tablename__ = "recurring_bills"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    source_bill_id = db.Column(db.Integer, db.ForeignKey("vendor_bills.id"),
                               nullable=False, index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"))
    amount = db.Column(db.Numeric(15, 4), nullable=False, default=0)
    currency = db.Column(db.String(3), nullable=False, default="SAR")
    # interval = interval_count × interval_unit. So "monthly" = (1, MONTH),
    # "every 45 days" = (45, DAY), "quarterly" = (3, MONTH).
    interval_unit = db.Column(db.String(10), nullable=False)   # DAY|WEEK|MONTH|YEAR
    interval_count = db.Column(db.Integer, nullable=False, default=1)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)   # nullable = open-ended
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    source_bill = db.relationship("VendorBill", foreign_keys=[source_bill_id])
    vendor = db.relationship("Vendor", foreign_keys=[vendor_id])
    creator = db.relationship("User", foreign_keys=[created_by])
    overrides = db.relationship(
        "RecurringBillOverride", backref="recurring_bill",
        cascade="all, delete-orphan", lazy="select",
    )

    @property
    def label_ar(self):
        unit = {"DAY": "يوم", "WEEK": "أسبوع",
                "MONTH": "شهر", "YEAR": "سنة"}.get(self.interval_unit, "؟")
        if self.interval_count == 1:
            return f"كل {unit}"
        return f"كل {self.interval_count} {unit}"


class RecurringBillOverride(db.Model):
    __tablename__ = "recurring_bill_overrides"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    recurring_bill_id = db.Column(
        db.Integer,
        db.ForeignKey("recurring_bills.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    occurrence_date = db.Column(db.Date, nullable=False)
    action = db.Column(db.String(10), nullable=False)    # SKIP | AMEND
    amount = db.Column(db.Numeric(15, 4))    # only used for AMEND

    __table_args__ = (
        db.UniqueConstraint("recurring_bill_id", "occurrence_date",
                             name="uq_recurring_override_date"),
    )
