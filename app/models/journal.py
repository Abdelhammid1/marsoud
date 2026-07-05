from datetime import datetime, date
from app import db


class JournalEntry(db.Model):
    __tablename__ = "journal_entries"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    number = db.Column(db.String(20), index=True)
    date = db.Column(db.Date, nullable=False, default=date.today)
    description = db.Column(db.Text, nullable=False)
    reference = db.Column(db.String(50))
    currency = db.Column(db.String(3), default="SAR")
    exchange_rate = db.Column(db.Numeric(10, 6), default=1.0)
    is_reversal = db.Column(db.Boolean, default=False)
    reversal_of = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    source_type = db.Column(db.String(30))
    source_id = db.Column(db.Integer)
    cashflow_category = db.Column(db.String(15))  # OPERATING / INVESTING / FINANCING / NONCASH (manual override)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Pause/reactivate — paused entries are excluded from reports
    is_active = db.Column(db.Boolean, default=True, index=True)
    pause_reason = db.Column(db.Text)
    paused_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    paused_at = db.Column(db.DateTime)
    reactivate_reason = db.Column(db.Text)
    reactivated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reactivated_at = db.Column(db.DateTime)

    paused_by = db.relationship("User", foreign_keys=[paused_by_id])
    reactivated_by = db.relationship("User", foreign_keys=[reactivated_by_id])

    company = db.relationship("Company", backref=db.backref("journal_entries", lazy="dynamic"))
    lines = db.relationship("JournalLine", backref="entry", cascade="all, delete-orphan")
    creator = db.relationship("User", foreign_keys=[created_by])

    @property
    def total_debit(self):
        return sum(float(l.debit or 0) for l in self.lines)

    @property
    def total_credit(self):
        return sum(float(l.credit or 0) for l in self.lines)

    @property
    def is_balanced(self):
        return abs(self.total_debit - self.total_credit) < 0.0001


class JournalLine(db.Model):
    __tablename__ = "journal_lines"
    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False, index=True)
    debit = db.Column(db.Numeric(15, 4), default=0)
    credit = db.Column(db.Numeric(15, 4), default=0)
    debit_base = db.Column(db.Numeric(15, 4), default=0)
    credit_base = db.Column(db.Numeric(15, 4), default=0)
    memo = db.Column(db.Text)

    account = db.relationship("Account", backref=db.backref("lines", lazy="dynamic"))
