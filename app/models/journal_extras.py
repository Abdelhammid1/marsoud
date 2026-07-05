import enum
from datetime import datetime, date
from app import db


class JournalAction(enum.Enum):
    CREATED = "CREATED"
    PAUSED = "PAUSED"
    REACTIVATED = "REACTIVATED"
    REVERSED = "REVERSED"
    EXPORTED = "EXPORTED"


class JournalAudit(db.Model):
    """Audit trail entry recording every action taken on a journal entry."""
    __tablename__ = "journal_audits"
    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.Enum(JournalAction), nullable=False)
    reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    user = db.relationship("User")
    entry = db.relationship("JournalEntry", backref="audit_logs")


class JournalTemplate(db.Model):
    __tablename__ = "journal_templates"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company", backref="journal_templates")
    lines = db.relationship("JournalTemplateLine", backref="template", cascade="all, delete-orphan")


class JournalTemplateLine(db.Model):
    __tablename__ = "journal_template_lines"
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("journal_templates.id"), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    debit = db.Column(db.Numeric(15, 4), default=0)
    credit = db.Column(db.Numeric(15, 4), default=0)
    memo = db.Column(db.Text)

    account = db.relationship("Account")


class RecurrenceFrequency(enum.Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"


class RecurringJournal(db.Model):
    """A schedule that posts the same journal entry from a template at intervals."""
    __tablename__ = "recurring_journals"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    template_id = db.Column(db.Integer, db.ForeignKey("journal_templates.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    frequency = db.Column(db.Enum(RecurrenceFrequency), nullable=False)
    next_run_date = db.Column(db.Date, nullable=False, index=True)
    end_date = db.Column(db.Date)
    is_active = db.Column(db.Boolean, default=True)
    is_deleted = db.Column(db.Boolean, default=False)  # soft-delete; row preserved for log history
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    template = db.relationship("JournalTemplate")
    company = db.relationship("Company")


class RecurringAction(enum.Enum):
    EXECUTE = "EXECUTE"
    FAIL = "FAIL"
    EDIT = "EDIT"
    STOP = "STOP"
    RESUME = "RESUME"
    DELETE = "DELETE"


class RecurringJournalLog(db.Model):
    """Audit log entry recording an action taken on a recurring journal schedule."""
    __tablename__ = "recurring_journal_logs"
    id = db.Column(db.Integer, primary_key=True)
    recurring_id = db.Column(db.Integer, db.ForeignKey("recurring_journals.id"), nullable=False, index=True)
    action = db.Column(db.Enum(RecurringAction), nullable=False)
    period_posted = db.Column(db.Date)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    error_message = db.Column(db.Text)
    reason = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    schedule = db.relationship("RecurringJournal", backref=db.backref("logs", cascade="all, delete-orphan"))
    user = db.relationship("User")
    journal_entry = db.relationship("JournalEntry")
