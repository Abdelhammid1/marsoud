"""MARSOUD-ACCOUNTING-OPS — general accounting operations, as wizards.

Adding capital, recording an opening balance or logging owner drawings used
to mean opening "قيد جديد" and knowing which account to debit and which to
credit. That asks for accounting knowledge on simple operations and invites
wrong entries.

Here each operation declares only what it needs from the user; the system
picks the accounts and posts the journal.

ADDING A NEW OPERATION
======================
Append one `Operation` to `OPERATIONS` below and write its `build`. That is
all — the index cards, the form, the POST handler, the routing and the
sidebar are all driven from this registry, so no new page, route or sidebar
item is ever needed.

Operations that move money use `Field(..., "financial_account")`, which
offers the cash account and every bank account the company actually has —
postable ones only, so header 1120 can never be chosen and fail at post
time. It is shared by every wizard, so a new one inherits it for free.

Two things a new operation must not forget:
  1. a `source_type` of its own, registered in
     app/services/source_reference.py — otherwise the ledger labels the
     entry "قيد يدوي" and the source-type coverage audit fails.
  2. if it ever writes a domain row beside the journal, add a branch to
     `_undo_source_side_effects` in app/services/ledger.py so reversing
     the journal rolls that row back too. The operations here write only
     a journal, so reversal already works.

Out of scope, deliberately: anything with its own module (invoices, bills,
payroll, assets, inventory, returns) stays in its own screen.
"""
from datetime import date, datetime

from app.services.ledger import (
    post_journal, get_account_by_code, resolve_financial_account, LedgerError,
)


# ─── Field spec ─────────────────────────────────────────────────────────
class Field:
    """One input on a wizard form.

    kind drives rendering in the shared template — a new operation that
    reuses these kinds needs no template change at all:
      amount             numeric, > 0, required
      date               date picker, defaults to today
      financial_account  the cash/bank account the money moved through
      textarea           free notes
    """

    def __init__(self, name, label, kind, required=False, help_text=None):
        self.name = name
        self.label = label
        self.kind = kind
        self.required = required
        self.help_text = help_text


# Valid values for Operation.cashflow_category — the same four the
# classifier accepts (services/reports.py) and the manual journal form
# offers.
CASHFLOW_CATEGORIES = ("OPERATING", "INVESTING", "FINANCING", "NONCASH")


class Operation:
    def __init__(self, key, title, icon, description, source_type, fields,
                 build, cashflow_category, effect=None):
        self.key = key
        self.title = title
        self.icon = icon
        self.description = description
        self.source_type = source_type
        self.fields = fields
        self.build = build
        # MARSOUD-OPS-FOUNDATION (2026-08-05) — REQUIRED, and positional
        # on purpose so a new operation cannot forget it.
        #
        # The classifier falls back to guessing from account codes when an
        # entry says nothing, and the three original operations happened to
        # guess right. That is luck, not design: a transfer between two
        # money accounts guesses OPERATING and would add imaginary cash to
        # the statement on every transfer. Every operation states its own.
        if cashflow_category not in CASHFLOW_CATEGORIES:
            raise ValueError(
                f"operation {key!r}: cashflow_category must be one of "
                f"{CASHFLOW_CATEGORIES}, got {cashflow_category!r}")
        self.cashflow_category = cashflow_category
        # One line shown on the form explaining the accounting effect.
        self.effect = effect


class OperationError(Exception):
    """User-facing validation error inside a wizard."""


# ─── Shared helpers ─────────────────────────────────────────────────────
def _money_account(company_id, account_id):
    """Resolve the account the money actually moved through.

    MARSOUD-FINANCIAL-ACCOUNT-FIELD (2026-08-04) — this used to resolve a
    PaymentMethod. Two things were wrong with that. A blank value fell
    back to code 1110, and the seeded "نقدي" method points at 1110 too,
    so the dropdown showed the same account twice under two names. And
    the seeded "bank" method points at one hardcoded bank (1124/CIB,
    because header 1120 refuses journal lines) — so capital injected into
    بنك مصر was posted to CIB. That is a wrong balance, not a wrong label.

    These operations have no customer, no supplier and no invoice; they
    are not payments. The question is only "which account", so the field
    asks that directly.
    """
    try:
        return resolve_financial_account(company_id, account_id)
    except LedgerError as e:
        raise OperationError(str(e))


def _equity_account(company_id, code, label):
    acc = get_account_by_code(company_id, code)
    if not acc:
        raise OperationError(
            f"حساب {label} ({code}) غير موجود — راجع شجرة الحسابات")
    return acc


def _amount(data, name="amount"):
    try:
        val = round(float(data.get(name) or 0), 2)
    except (TypeError, ValueError):
        raise OperationError("المبلغ غير صالح")
    if val <= 0:
        raise OperationError("المبلغ يجب أن يكون أكبر من صفر")
    return val


def _date(data, name="date"):
    raw = (data.get(name) or "").strip()
    if not raw:
        return date.today()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise OperationError("التاريخ غير صالح")


def _note(data, name="notes"):
    return (data.get(name) or "").strip() or None


# ─── The operations ─────────────────────────────────────────────────────
def _build_capital(company_id, data):
    """Dr cash/bank · Cr 3100 رأس المال."""
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    capital = _equity_account(company_id, "3100", "رأس المال")
    note = _note(data)
    desc = f"إضافة رأس مال — {money_label}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": money.id, "debit": amount, "credit": 0,
         "memo": "إيداع رأس مال"},
        {"account_id": capital.id, "debit": 0, "credit": amount,
         "memo": note or "رأس المال"},
    ]


def _build_opening_balance(company_id, data):
    """Dr cash/bank · Cr 3900 حساب الافتتاح."""
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    opening = _equity_account(company_id, "3900", "حساب الافتتاح")
    note = _note(data)
    desc = f"رصيد افتتاحي — {money_label}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": money.id, "debit": amount, "credit": 0,
         "memo": "رصيد افتتاحي"},
        {"account_id": opening.id, "debit": 0, "credit": amount,
         "memo": note or "حساب الافتتاح"},
    ]


def _build_owner_drawings(company_id, data):
    """Dr 3200 جاري الشركاء · Cr cash/bank — reduces equity."""
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    drawings = _equity_account(company_id, "3200", "جاري الشركاء / المسحوبات")
    note = _note(data)
    desc = f"مسحوبات المالك — {money_label}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": drawings.id, "debit": amount, "credit": 0,
         "memo": note or "مسحوبات المالك"},
        {"account_id": money.id, "debit": 0, "credit": amount,
         "memo": f"صرف من {money_label}"},
    ]


OPERATIONS = [
    Operation(
        key="capital",
        title="إضافة رأس مال",
        icon="💰",
        description="عند ضخ المالك أموالاً جديدة داخل الشركة. يمكن تنفيذها أكثر من مرة.",
        effect="يزيد النقدية/البنك ويزيد رأس المال (3100).",
        source_type="capital_injection",
        # owner puts money in — a financing activity
        cashflow_category="FINANCING",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ العملية", "date", required=True),
            Field("account_id", "الحساب المالي", "financial_account",
                  required=True,
                  help_text="الحساب الذي دخلت فيه الأموال — الصندوق أو أحد البنوك"),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_capital,
    ),
    Operation(
        key="opening-balance",
        title="تسجيل رصيد افتتاحي",
        icon="📂",
        description="لتسجيل أرصدة شركة كانت تعمل قبل بدء استخدام مرصود.",
        effect="يزيد النقدية/البنك مقابل حساب الافتتاح (3900).",
        source_type="opening_balance",
        # opening equity — financing, same as capital
        cashflow_category="FINANCING",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "التاريخ", "date", required=True),
            Field("account_id", "الحساب المالي", "financial_account",
                  required=True,
                  help_text="الحساب الذي يحمل الرصيد الافتتاحي"),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_opening_balance,
    ),
    Operation(
        key="owner-drawings",
        title="مسحوبات المالك",
        icon="🏧",
        description="عند سحب المالك أموالاً من الشركة لاستخدامه الشخصي.",
        effect="يخفض حقوق الملكية عبر جاري الشركاء (3200) ويخفض النقدية/البنك.",
        source_type="owner_drawings",
        # owner takes money out — a financing activity
        cashflow_category="FINANCING",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "التاريخ", "date", required=True),
            Field("account_id", "السحب من", "financial_account",
                  required=True,
                  help_text="الحساب الذي خرجت منه الأموال"),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_owner_drawings,
    ),
]

OPERATIONS_BY_KEY = {op.key: op for op in OPERATIONS}


def get_operation(key):
    return OPERATIONS_BY_KEY.get(key)


# ─── The one executor every wizard goes through ─────────────────────────
def run_operation(op, company_id, data, actor_id=None):
    """Build the operation's lines and post them. Returns the JournalEntry.

    post_journal validates balance, tenant ownership and postability, and
    commits — so a wizard cannot produce an unbalanced or cross-tenant
    entry no matter what it builds.
    """
    entry_date = _date(data)
    description, lines = op.build(company_id, data)
    try:
        return post_journal(
            company_id=company_id,
            description=description,
            lines=lines,
            entry_date=entry_date,
            reference=op.key.upper(),
            created_by=actor_id,
            source_type=op.source_type,
            cashflow_category=op.cashflow_category,
        )
    except LedgerError as e:
        # Surface ledger complaints as the wizard's own error type so the
        # route only has to catch one thing.
        raise OperationError(str(e))
