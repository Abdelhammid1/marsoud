"""MARSOUD-CASH-CUSTODY-01 (2026-08-07) — cash custody models.

Distinct from advances (`app/models/advances.py`). Advances are
salary-deducted loans; custody is operational cash handed to an
employee or department against future receipts + return of the
excess / collection of the shortfall.

Three tables, mirroring the advances shape:

  CashCustodyRequest   — employee/manager asking for cash for a
                         purpose. PENDING → APPROVED / REJECTED.
  CashCustody          — the live custody after issue. ISSUED →
                         PARTIALLY_SETTLED → SETTLED / CANCELLED.
  CashCustodySettlementLine — one row per expense receipt during
                         settlement. Accumulates without posting;
                         the full-settlement journal is posted
                         once at `close_settlement`.

Holder polymorphism: either Employee OR Department (mirrors what
the ticket names). Two nullable FKs + a CHECK constraint that
enforces "exactly one" at the DB level, so a crafted POST can't
stitch both together. Same shape as Task's assigned_to_id /
department_id.

Every mutation MUST go through `app/services/cash_custody.py`.
Direct model writes bypass the journal posting + status flip
rules and will silently corrupt the balance.
"""
import enum
from datetime import datetime
from app import db


# ─── Enums ─────────────────────────────────────────────────────
class CustodyHolderType(enum.Enum):
    EMPLOYEE = "EMPLOYEE"
    DEPARTMENT = "DEPARTMENT"


class CustodyRequestStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class EffectiveRequestStatus:
    """MARSOUD-CUSTODY-DELETE-CONSISTENCY (2026-08-12) —
    screen-visible status for a CashCustodyRequest. NOT an
    enum stored in the DB — it's a pure derivation off the
    request's raw status + the linked custody's state.

    Purpose: `req.status` records what the accountant DECIDED
    (approved / rejected). This class expresses what
    ULTIMATELY HAPPENED — most importantly, distinguishing
    "approved custody was later cancelled" from "still
    approved and live", without mutating req.status (and
    without an ALTER TYPE migration for a new enum value).

    Tuple shape used by templates: (code, label_ar, badge_class).
    """
    PENDING = ("PENDING", "قيد الانتظار", "badge-draft")
    APPROVED = ("APPROVED", "معتمدة", "badge-paid")
    REJECTED = ("REJECTED", "مرفوضة", "badge-cancelled")
    CANCELLED = ("CANCELLED", "ملغاة (بعد الاعتماد)",
                 "badge-cancelled")


class CustodyStatus(enum.Enum):
    ISSUED = "ISSUED"                        # money out, no settlement yet
    PARTIALLY_SETTLED = "PARTIALLY_SETTLED"  # some expense lines added
    SETTLED = "SETTLED"                      # closed, settlement journal posted
    CANCELLED = "CANCELLED"                  # issue journal reversed


class ShortfallDisposition(enum.Enum):
    """Where the accountant sends a shortfall (unsettled residual)
    at close-settlement time. The ticket names both:
      · EMPLOYEE_LIABILITY — push to the employee's 2130 sub-account,
        recovers via later payroll deduction or a manual receipt.
        Only valid when holder_type = EMPLOYEE.
      · EXPENSE — book as an operating expense ("عجز عهدة") under
        5xxx. Loss absorbed by the company. Valid for both holder
        types."""
    EMPLOYEE_LIABILITY = "EMPLOYEE_LIABILITY"
    EXPENSE = "EXPENSE"


# ─── CHECK-constraint SQL shared by request + custody ────────
# Exactly-one-holder guard. Enforces at DB level that either
# EMPLOYEE (employee_id set, department_id null) OR DEPARTMENT
# (department_id set, employee_id null) — never both, never
# neither. `holder_type` string values are cast because SQLAlchemy
# Enum columns store the .value.
_HOLDER_CHECK = (
    "(holder_type = 'EMPLOYEE' AND employee_id IS NOT NULL "
    "AND department_id IS NULL) "
    "OR "
    "(holder_type = 'DEPARTMENT' AND department_id IS NOT NULL "
    "AND employee_id IS NULL)"
)


# ─── CashCustodyRequest ────────────────────────────────────────
class CashCustodyRequest(db.Model):
    """A request for a cash custody. Mirrors AdvanceRequest.

    Employee submits from /my/custody (portal), or an accountant
    creates it directly on behalf of a department. Approval flips
    to APPROVED and simultaneously issues the custody (single
    transaction inside services/cash_custody.approve_custody_request).
    """
    __tablename__ = "cash_custody_requests"
    __table_args__ = (
        db.CheckConstraint(_HOLDER_CHECK,
                            name="ck_custody_request_one_holder"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id", ondelete="CASCADE"),
                            nullable=False, index=True)

    holder_type = db.Column(db.Enum(CustodyHolderType),
                             nullable=False, index=True)
    employee_id = db.Column(db.Integer,
                             db.ForeignKey("employees.id",
                                            ondelete="SET NULL"),
                             nullable=True, index=True)
    department_id = db.Column(db.Integer,
                               db.ForeignKey("departments.id",
                                              ondelete="SET NULL"),
                               nullable=True, index=True)

    amount = db.Column(db.Numeric(15, 2), nullable=False)
    purpose = db.Column(db.Text, nullable=False)
    needed_by_date = db.Column(db.Date, nullable=True)

    status = db.Column(db.Enum(CustodyRequestStatus),
                        default=CustodyRequestStatus.PENDING,
                        nullable=False, index=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    review_note = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    company = db.relationship("Company")
    employee = db.relationship(
        "Employee", foreign_keys=[employee_id],
        backref=db.backref("custody_requests", lazy="dynamic"))
    department = db.relationship(
        "Department", foreign_keys=[department_id],
        backref=db.backref("custody_requests", lazy="dynamic"))
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    creator = db.relationship("User", foreign_keys=[created_by])

    @property
    def holder(self):
        """Resolve to the actual party row. Guaranteed exactly one
        by the CHECK constraint above."""
        return self.employee or self.department

    @property
    def holder_name(self):
        h = self.holder
        return h.name if h else "—"

    @property
    def effective_status(self):
        """MARSOUD-CUSTODY-DELETE-CONSISTENCY (2026-08-12) —
        screen-visible status. Returns an EffectiveRequestStatus
        3-tuple: (code, label_ar, badge_class).

        The rule that closes drift-A/B (cancel_custody +
        _undo_source_side_effects both leave req.status stale):
        when the raw status is APPROVED but the LINKED custody
        was later cancelled, this returns CANCELLED. Both the
        accountant's requests screen and the employee's portal
        read this property, so they always agree.

        Raw req.status remains a faithful record of the review
        decision (approved / rejected) — never mutated by
        cancel/reopen paths.
        """
        if self.status == CustodyRequestStatus.PENDING:
            return EffectiveRequestStatus.PENDING
        if self.status == CustodyRequestStatus.REJECTED:
            return EffectiveRequestStatus.REJECTED
        # APPROVED — check the linked custody's actual state.
        # `self.custody` is the backref set by CashCustody.request
        # relationship (uselist=False) below at line ~248.
        if (self.custody is not None
                and self.custody.status == CustodyStatus.CANCELLED):
            return EffectiveRequestStatus.CANCELLED
        return EffectiveRequestStatus.APPROVED


# ─── CashCustody ───────────────────────────────────────────────
class CashCustody(db.Model):
    """The live custody row. Mirrors EmployeeAdvance.

    Money left the till at `issued_on` (journal_entry_id posted).
    Settlement lines accumulate without posting until
    `close_settlement`, which posts ONE balanced journal covering
    all expenses + returned excess + shortfall.

    `settlement_due_date` is the soft deadline — cron sweep flips
    `custody_overdue_notified_at` to fire ONE reminder to the
    accountant per custody past that date. The status stays ISSUED /
    PARTIALLY_SETTLED — "overdue" is a computed property, not a
    fourth status (matches how invoices handle overdue).
    """
    __tablename__ = "cash_custodies"
    __table_args__ = (
        db.CheckConstraint(_HOLDER_CHECK,
                            name="ck_custody_one_holder"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id", ondelete="CASCADE"),
                            nullable=False, index=True)

    holder_type = db.Column(db.Enum(CustodyHolderType),
                             nullable=False, index=True)
    employee_id = db.Column(db.Integer,
                             db.ForeignKey("employees.id",
                                            ondelete="SET NULL"),
                             nullable=True, index=True)
    department_id = db.Column(db.Integer,
                               db.ForeignKey("departments.id",
                                              ondelete="SET NULL"),
                               nullable=True, index=True)

    amount_issued = db.Column(db.Numeric(15, 2), nullable=False)
    amount_settled = db.Column(db.Numeric(15, 2), nullable=False, default=0)
    amount_returned = db.Column(db.Numeric(15, 2), nullable=False, default=0)
    amount_shortfall = db.Column(db.Numeric(15, 2), nullable=False, default=0)

    status = db.Column(db.Enum(CustodyStatus),
                        default=CustodyStatus.ISSUED,
                        nullable=False, index=True)

    payment_method_id = db.Column(
        db.Integer,
        db.ForeignKey("payment_methods.id", ondelete="SET NULL"),
        nullable=True)
    purpose = db.Column(db.Text)
    issued_on = db.Column(db.Date, nullable=False)
    settlement_due_date = db.Column(db.Date, nullable=True, index=True)

    # Journal links. issue = disbursement, settlement = close-settle,
    # reversal = cancel.
    request_id = db.Column(db.Integer,
                            db.ForeignKey("cash_custody_requests.id",
                                          ondelete="SET NULL"),
                            nullable=True)
    journal_entry_id = db.Column(db.Integer,
                                  db.ForeignKey("journal_entries.id",
                                                ondelete="SET NULL"),
                                  nullable=True)
    settlement_journal_entry_id = db.Column(
        db.Integer,
        db.ForeignKey("journal_entries.id", ondelete="SET NULL"),
        nullable=True)
    reversal_entry_id = db.Column(
        db.Integer,
        db.ForeignKey("journal_entries.id", ondelete="SET NULL"),
        nullable=True)

    note = db.Column(db.Text)
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    settled_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    settled_at = db.Column(db.DateTime)
    cancelled_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancelled_at = db.Column(db.DateTime)
    cancel_reason = db.Column(db.Text)
    # Where shortfall was dispositioned at close (null before close).
    shortfall_disposition = db.Column(db.Enum(ShortfallDisposition),
                                       nullable=True)

    # Cron dedup: set to the timestamp of the FIRST overdue ping
    # so the sweep doesn't re-fire on every tick. Cleared on
    # cancel or settle. Matches vendor-bill-overdue idempotency
    # pattern (services/vendor_bills.update_overdue_vendor_bills).
    custody_overdue_notified_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company")
    employee = db.relationship(
        "Employee", foreign_keys=[employee_id],
        backref=db.backref("custodies", lazy="dynamic"))
    department = db.relationship(
        "Department", foreign_keys=[department_id],
        backref=db.backref("custodies", lazy="dynamic"))
    request = db.relationship(
        "CashCustodyRequest", foreign_keys=[request_id],
        backref=db.backref("custody", uselist=False))
    entry = db.relationship("JournalEntry",
                             foreign_keys=[journal_entry_id])
    settlement_entry = db.relationship(
        "JournalEntry", foreign_keys=[settlement_journal_entry_id])
    reversal_entry = db.relationship(
        "JournalEntry", foreign_keys=[reversal_entry_id])
    payment_method = db.relationship("PaymentMethod")
    approver = db.relationship("User", foreign_keys=[approved_by])
    creator = db.relationship("User", foreign_keys=[created_by])
    settler = db.relationship("User", foreign_keys=[settled_by])
    canceller = db.relationship("User", foreign_keys=[cancelled_by])

    @property
    def holder(self):
        return self.employee or self.department

    @property
    def holder_name(self):
        h = self.holder
        return h.name if h else "—"

    @property
    def amount_pending(self):
        """Money still floating — issued minus what's already
        landed on an expense account or come back to cash."""
        return round(float(self.amount_issued or 0)
                     - float(self.amount_settled or 0)
                     - float(self.amount_returned or 0), 2)

    @property
    def is_open(self):
        return self.status in (CustodyStatus.ISSUED,
                                CustodyStatus.PARTIALLY_SETTLED)

    @property
    def is_overdue(self):
        """True when past `settlement_due_date` and still open.
        Report + sidebar badge key off this."""
        if not self.settlement_due_date or not self.is_open:
            return False
        from datetime import date as _date
        return self.settlement_due_date < _date.today()


# ─── CashCustodySettlementLine ─────────────────────────────────
class CashCustodySettlementLine(db.Model):
    """One receipt line during settlement. Mirrors AdvanceRepayment
    (one row per event, cascade-deleted with the custody).

    IMPORTANT: adding a line does NOT post a journal. Journal posts
    once at `close_settlement`, aggregating lines by
    expense_account_id + adding the returned/shortfall legs. This
    matches the ticket's "accumulate then post one journal" rule.

    Receipt files attach via the polymorphic Document model
    (source_type=CASH_CUSTODY_SETTLEMENT, source_id=this row's id).
    That's the codebase's standard file-attachment pattern (see
    tasks + leads); a direct UserFile FK would tie the receipt to
    a specific uploader user, which breaks when an accountant
    uploads on behalf of an employee.
    """
    __tablename__ = "cash_custody_settlement_lines"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    custody_id = db.Column(db.Integer,
                            db.ForeignKey("cash_custodies.id",
                                          ondelete="CASCADE"),
                            nullable=False, index=True)
    # The expense account this receipt line hits when settlement
    # closes. Must be postable + company-scoped (validated in
    # services/cash_custody.add_settlement_line).
    expense_account_id = db.Column(db.Integer,
                                    db.ForeignKey("accounts.id",
                                                  ondelete="RESTRICT"),
                                    nullable=False)

    amount = db.Column(db.Numeric(15, 2), nullable=False)
    receipt_note = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    custody = db.relationship(
        "CashCustody",
        backref=db.backref("settlement_lines", lazy="selectin",
                           order_by="CashCustodySettlementLine.id.asc()",
                           cascade="all, delete-orphan"))
    expense_account = db.relationship(
        "Account", foreign_keys=[expense_account_id])
    creator = db.relationship("User", foreign_keys=[created_by])

    def __repr__(self):                                  # pragma: no cover
        return (f"<CashCustodySettlementLine custody={self.custody_id} "
                f"acc={self.expense_account_id} amt={self.amount}>")
