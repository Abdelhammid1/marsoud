"""MARSOUD-TKT-HR-DECISIONS-01 (2026-09-02) — قرارات الموظفين.

A first-class "decision document" per employee. Predates and outlives
its financial side-effect: the row exists from the moment the
decision is drafted (`DRAFT`), and stays visible in the audit log
whether it was later executed (JE posted / employee terminated /
payroll adjusted) or cancelled with a reason.

Deliberately NOT trying to be the source of truth for employee
fields — an administrative decision (promotion, transfer) is
recorded here; the accompanying edit still happens through the
existing `update_employee` path so `EmployeeHistory` keeps its
current shape. This is a decision LAYER, not a replacement.
"""
import enum
from datetime import datetime
from app import db


# ─── Enums (stored as short strings) ────────────────────────────
class HrDecisionKind(enum.Enum):
    APPOINTMENT = "APPOINTMENT"      # تعيين
    PROMOTION = "PROMOTION"          # ترقية
    TRANSFER = "TRANSFER"            # نقل قسم / فرع
    WARNING = "WARNING"              # إنذار كتابي
    PENALTY = "PENALTY"              # جزاء مالي
    BONUS = "BONUS"                  # مكافأة
    TERMINATION = "TERMINATION"      # إنهاء خدمة


class HrDecisionStatus(enum.Enum):
    DRAFT = "DRAFT"                      # created, awaits execution
    EXECUTED = "EXECUTED"                # side-effect posted
    PENDING_PAYROLL = "PENDING_PAYROLL"  # queued for next payroll run (Phase 2)
    CANCELLED = "CANCELLED"              # voided before execution


class HrDecisionTiming(enum.Enum):
    IMMEDIATE = "IMMEDIATE"          # posts JE at execute-time
    NEXT_PAYROLL = "NEXT_PAYROLL"    # folded into the next payroll run


# ─── Category classification (derived, not persisted) ──────────
_ADMIN_KINDS = frozenset({
    HrDecisionKind.APPOINTMENT,
    HrDecisionKind.PROMOTION,
    HrDecisionKind.TRANSFER,
    HrDecisionKind.WARNING,
})
_FINANCIAL_KINDS = frozenset({
    HrDecisionKind.PENALTY,
    HrDecisionKind.BONUS,
})


def kind_category(kind):
    """Return 'ADMIN' | 'FINANCIAL' | 'TERMINATION' for a kind.

    Category drives the execute-time dispatch. ADMIN never posts a
    JE (Phase 1). FINANCIAL posts a JE only when timing=IMMEDIATE;
    NEXT_PAYROLL waits for Phase 2's payroll fold. TERMINATION
    delegates to `terminate_employee` (never posts a JE — pro-rating
    happens when payroll runs the month of the leave date).
    """
    if kind == HrDecisionKind.TERMINATION:
        return "TERMINATION"
    if kind in _ADMIN_KINDS:
        return "ADMIN"
    if kind in _FINANCIAL_KINDS:
        return "FINANCIAL"
    return "ADMIN"  # safe default for unknown kinds


class HrDecision(db.Model):
    __tablename__ = "hr_decisions"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(
        db.Integer, db.ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True)
    employee_id = db.Column(
        db.Integer, db.ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False, index=True)

    kind = db.Column(db.String(30), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False,
                        default=HrDecisionStatus.DRAFT.value,
                        index=True)
    timing = db.Column(db.String(20), nullable=False,
                        default=HrDecisionTiming.IMMEDIATE.value)

    effective_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Numeric(15, 2), nullable=True)
    payment_account_id = db.Column(
        db.Integer, db.ForeignKey("accounts.id"), nullable=True)

    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=True)
    reference = db.Column(db.String(100), nullable=True)

    journal_entry_id = db.Column(
        db.Integer, db.ForeignKey("journal_entries.id",
                                    ondelete="SET NULL"),
        nullable=True, index=True)
    payroll_run_id = db.Column(
        db.Integer, db.ForeignKey("payroll_runs.id",
                                    ondelete="SET NULL"),
        nullable=True, index=True)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"),
                            nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    executed_by = db.Column(db.Integer, db.ForeignKey("users.id"),
                             nullable=True)
    executed_at = db.Column(db.DateTime, nullable=True)
    cancelled_by = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    cancel_reason = db.Column(db.Text, nullable=True)

    employee = db.relationship("Employee", foreign_keys=[employee_id])
    journal_entry = db.relationship(
        "JournalEntry", foreign_keys=[journal_entry_id])
    payment_account = db.relationship(
        "Account", foreign_keys=[payment_account_id])

    # ─── Derived helpers ─────────────────────────────────────
    @property
    def kind_enum(self):
        try:
            return HrDecisionKind(self.kind)
        except ValueError:
            return None

    @property
    def category(self):
        """'ADMIN' / 'FINANCIAL' / 'TERMINATION'."""
        k = self.kind_enum
        return kind_category(k) if k else "ADMIN"

    @property
    def is_immutable(self):
        """AC #8 — an executed decision cannot be edited or cancelled.
        The only "undo" is a new inverse decision."""
        return self.status == HrDecisionStatus.EXECUTED.value

    @property
    def kind_ar(self):
        return {
            "APPOINTMENT": "تعيين",
            "PROMOTION":   "ترقية",
            "TRANSFER":    "نقل",
            "WARNING":     "إنذار",
            "PENALTY":     "جزاء مالي",
            "BONUS":       "مكافأة",
            "TERMINATION": "إنهاء خدمة",
        }.get(self.kind, self.kind or "—")

    @property
    def status_ar(self):
        return {
            "DRAFT":            "مسودة",
            "EXECUTED":         "منفّذ",
            "PENDING_PAYROLL":  "معلّق للراتب القادم",
            "CANCELLED":        "ملغى",
        }.get(self.status, self.status or "—")
