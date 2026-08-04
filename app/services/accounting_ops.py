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

from app import db
from app.services.open_items import OpenItemError
from app.services.ledger import (
    post_journal, get_account_by_code, resolve_financial_account, LedgerError,
)


# MARSOUD-OPS-FOUNDATION — every kind the shared template can render.
# A typo in a Field kind used to fall through to a plain text input and
# ship a broken wizard silently; Field now refuses an unknown kind.
FIELD_KINDS = frozenset({
    "amount", "date", "textarea",
    "financial_account", "financial_account_to",
    "expense_account", "revenue_account",
    "party", "open_item",
})

# Which chart-of-accounts root each account kind draws from.
ACCOUNT_KIND_ROOTS = {
    "expense_account": "5000",
    "revenue_account": "4000",
}


# ─── Field spec ─────────────────────────────────────────────────────────
class Field:
    """One input on a wizard form.

    kind drives rendering in the shared template — a new operation that
    reuses these kinds needs no template change at all:

      amount                 numeric, > 0, required
      date                   date picker, defaults to today
      textarea               free notes
      financial_account      the cash/bank account money moved through
      financial_account_to   a SECOND money account on the same screen,
                             for the transfer (from → to)
      expense_account        a postable expense account (under 5000)
      revenue_account        a postable revenue account (under 4000)
      party                  a customer / vendor / employee. Resolves to
                             the party's SUB-account, never the header —
                             every party operation in the system runs on
                             that structure, and the header refuses lines
      open_item              one of the amounts still owed, so a payment
                             is always tied to what it settles

    Every account kind offers POSTABLE accounts only. That is enforced in
    ledger.postable_under, not re-implemented per kind.
    """

    def __init__(self, name, label, kind, required=False, help_text=None,
                 item_kind=None):
        if kind not in FIELD_KINDS:
            raise ValueError(
                f"field {name!r}: unknown kind {kind!r} — expected one of "
                f"{sorted(FIELD_KINDS)}")
        self.name = name
        self.label = label
        self.kind = kind
        self.required = required
        self.help_text = help_text
        # open_item only: which family of items this picker offers. A
        # settle-accrued-expense wizard must not list dividends payable.
        self.item_kind = item_kind


# Valid values for Operation.cashflow_category — the same four the
# classifier accepts (services/reports.py) and the manual journal form
# offers.
CASHFLOW_CATEGORIES = ("OPERATING", "INVESTING", "FINANCING", "NONCASH")


# ─── Hard boundaries (MARSOUD-OPS-FOUNDATION §5) ────────────────────────
# Two things a quick wizard must never be allowed to do, because in both
# cases the journal still BALANCES — nothing looks wrong, no error is
# raised, and the damage only surfaces later in a report nobody
# reconciles.
#
#   party  an expense with a supplier belongs in the bills module, which
#          drives the vendor sub-account under 2110. Posting it straight
#          to cash leaves the vendor's statement understating what was
#          paid — the supplier balance and the ledger disagree.
#
#   tax    an expense carrying input VAT belongs in purchases, which
#          drives 1280. Posting the gross to the expense account inflates
#          the expense and loses the reclaimable VAT entirely.
#
# HONEST SCOPE NOTE: the ticket's acceptance criterion is about the
# GENERIC expense/revenue operations, which this ticket does not build.
# The guards and their tests land now so that those operations inherit
# them the day they are written; today they are demonstrated on the
# accrual pair. The criterion cannot be fully shown until the generic
# operations exist.
BOUNDARIES = ("party", "tax")

# Field names that mean "a counterparty is involved" / "tax is involved".
# Matched as substrings of the submitted key, so vendor_id, supplier_id
# and party both trip the party guard.
_BOUNDARY_KEYS = {
    "party": ("party", "vendor", "supplier", "customer", "employee"),
    "tax": ("tax", "vat"),
}

_BOUNDARY_MESSAGES = {
    "party": ("هذه العملية لا تقبل طرفًا (مورد/عميل). المصروف المرتبط "
              "بمورد يُسجَّل من «فواتير الموردين» حتى يظهر في كشف حساب "
              "المورد."),
    "tax": ("هذه العملية لا تقبل ضريبة. المصروف الذي يحمل ضريبة قيمة "
            "مضافة يُسجَّل من «المشتريات» حتى تُرحَّل الضريبة إلى حسابها."),
}


def enforce_boundaries(op, data):
    """Refuse a submission that carries something the operation forbids.

    Checked on the SUBMITTED DATA, not just on the declared fields: the
    form never offers these, so the only way one arrives is a crafted
    POST — which is exactly the case worth refusing.
    """
    for boundary in op.forbids:
        for key in data.keys():
            low = key.lower()
            if any(t in low for t in _BOUNDARY_KEYS[boundary]):
                # An empty value is someone's stray form field, not an
                # attempt to book a party or tax. Only a real value is a
                # boundary crossing.
                if (data.get(key) or "").strip():
                    raise OperationError(_BOUNDARY_MESSAGES[boundary])


class Operation:
    def __init__(self, key, title, icon, description, source_type, fields,
                 build, cashflow_category, effect=None, forbids=()):
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
        # guess right. That is luck, not design — measured on the seeded
        # tree:
        #
        #   settle-accrued-expense  inference says FINANCING, because the
        #     payable is 2160 and the rule is `code.startswith("21") ->
        #     FINANCING`. It is a real 500 of cash landing in the wrong
        #     section: paying an accrued operating expense is operating.
        #     Declaring OPERATING moves it back.
        #
        #   transfer                inference says OPERATING, but both legs
        #     are cash so the entry nets to zero and contributes nothing
        #     whatever it is called. NONCASH is the honest label rather
        #     than a fix — it is what the entry shows the user, and it
        #     stops relying on that arithmetic accident.
        #
        # Every operation states its own.
        if cashflow_category not in CASHFLOW_CATEGORIES:
            raise ValueError(
                f"operation {key!r}: cashflow_category must be one of "
                f"{CASHFLOW_CATEGORIES}, got {cashflow_category!r}")
        self.cashflow_category = cashflow_category
        # One line shown on the form explaining the accounting effect.
        self.effect = effect

        # MARSOUD-OPS-FOUNDATION §5 — the boundaries this operation
        # refuses to cross. Validated here so a typo is a startup error
        # rather than a guard that silently never fires.
        for boundary in forbids:
            if boundary not in BOUNDARIES:
                raise ValueError(
                    f"operation {key!r}: unknown boundary {boundary!r} — "
                    f"expected one of {BOUNDARIES}")
        if "party" in forbids:
            # Refusing a party while ASKING for one would refuse every
            # submission. Catch the contradiction at build time.
            party_fields = [f.name for f in fields if f.kind == "party"]
            if party_fields:
                raise ValueError(
                    f"operation {key!r} forbids a party but declares "
                    f"{party_fields} — it would refuse every submission")
        self.forbids = tuple(forbids)


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


def _account_under(company_id, kind, account_id, label):
    """Resolve an expense/revenue picker to a postable account."""
    from app.services.ledger import resolve_account_under
    root = ACCOUNT_KIND_ROOTS[kind]
    try:
        return resolve_account_under(
            company_id, root, account_id,
            missing_msg=f"اختر {label}",
            invalid_msg=f"{label} غير صالح")
    except LedgerError as e:
        raise OperationError(str(e))


# ─── The party picker ───────────────────────────────────────────────────
# Submitted as "<type>:<id>" so one <select> can mix all three without a
# second field telling us which table to look in.
PARTY_TYPES = ("customer", "vendor", "employee")


def party_choices(company_id):
    """[(group_label_ar, [(value, label), ...]), ...] for the picker."""
    from app.models import Customer, Vendor
    from app.models.payroll import Employee
    groups = []
    for ptype, model, label, order in (
        ("customer", Customer, "العملاء", "name"),
        ("vendor", Vendor, "الموردون", "name"),
        ("employee", Employee, "الموظفون", "name"),
    ):
        q = model.query.filter_by(company_id=company_id)
        if hasattr(model, "is_active"):
            q = q.filter(model.is_active.is_(True))
        rows = q.order_by(getattr(model, order)).all()
        if rows:
            groups.append((label, [(f"{ptype}:{r.id}", r.name) for r in rows]))
    return groups


def resolve_party(company_id, raw):
    """'<type>:<id>' → (party, sub_account, label).

    Returns the party's OWN sub-account under 1130 / 2110 / 2130 — never
    the header. Every party operation in the system posts to that leaf,
    and the header is not postable anyway, so returning it would fail at
    save time with a confusing message.
    """
    from app.models import Customer, Vendor
    from app.models.payroll import Employee
    from app.services.subsidiary import (
        ensure_customer_account, ensure_vendor_account,
        ensure_employee_account,
    )
    ptype, _, pid = (raw or "").partition(":")
    if ptype not in PARTY_TYPES or not pid.isdigit():
        raise OperationError("اختر الطرف")
    model, ensure = {
        "customer": (Customer, ensure_customer_account),
        "vendor": (Vendor, ensure_vendor_account),
        "employee": (Employee, ensure_employee_account),
    }[ptype]
    party = db.session.get(model, int(pid))
    if not party or party.company_id != company_id:
        raise OperationError("الطرف المختار غير صالح")
    account = ensure(party)
    if account is None:
        raise OperationError("تعذر إنشاء الحساب الفرعي للطرف")
    return party, account, party.name


# Kinds the shared template renders as a grouped <select>. Everything
# else is a plain input.
SELECT_KINDS = frozenset({
    "financial_account", "financial_account_to",
    "expense_account", "revenue_account", "party", "open_item",
})

# Shown in place of an empty picker, so "no options" reads as a setup
# problem rather than a broken page.
EMPTY_PICKER_MESSAGES = {
    "financial_account": "لا يوجد حساب نقدية أو بنك قابل للترحيل — راجع شجرة الحسابات.",
    "financial_account_to": "لا يوجد حساب نقدية أو بنك قابل للترحيل — راجع شجرة الحسابات.",
    "expense_account": "لا يوجد حساب مصروفات قابل للترحيل — راجع شجرة الحسابات.",
    "revenue_account": "لا يوجد حساب إيرادات قابل للترحيل — راجع شجرة الحسابات.",
    "party": "لا يوجد عملاء أو موردون أو موظفون مسجّلون.",
    "open_item": "لا توجد بنود مفتوحة تحتاج سداد.",
}


def _accounts_as_choices(groups):
    """[(label, [Account…])…] → [(label, [(id, name)…])…]."""
    return [(label, [(a.id, a.name_ar or a.name) for a in accounts])
            for label, accounts in groups if accounts]


def field_choices(op, company_id):
    """{field_name: [(group_label, [(value, label), ...]), ...]}.

    Built per operation so the template stays dumb: it renders whatever
    list it is handed and never learns where any of them come from.
    """
    from app.services.ledger import cash_and_bank_accounts, postable_under
    out = {}
    for f in op.fields:
        if f.kind not in SELECT_KINDS:
            continue
        if f.kind in ("financial_account", "financial_account_to"):
            out[f.name] = _accounts_as_choices(
                cash_and_bank_accounts(company_id))
        elif f.kind in ACCOUNT_KIND_ROOTS:
            root = ACCOUNT_KIND_ROOTS[f.kind]
            accounts = postable_under(company_id, root)
            out[f.name] = _accounts_as_choices([(f.label, accounts)])
        elif f.kind == "party":
            out[f.name] = party_choices(company_id)
        elif f.kind == "open_item":
            from app.services.open_items import open_item_choices
            out[f.name] = open_item_choices(company_id, kind=f.item_kind)
    return out


def _account_by_code(company_id, code, label):
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


def _optional_date(data, name):
    """A date field that may legitimately be left empty (a due date)."""
    if not (data.get(name) or "").strip():
        return None
    return _date(data, name)


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


# ─── What a builder may return ──────────────────────────────────────────
class Built:
    """A builder's answer when it needs more than lines.

    MARSOUD-OPS-FOUNDATION (2026-08-05). Two-sided operations write a row
    BEFORE the journal exists — the open item has to have an id for the
    journal's source_id to point at it. So the builder creates and flushes
    the row, hands back its id as `source_id`, and gets a callback once
    the entry exists to store the link the other way.

    Builders that just move money keep returning a plain (desc, lines)
    tuple; run_operation accepts both.
    """

    def __init__(self, description, lines, source_id=None, after_post=None):
        self.description = description
        self.lines = lines
        self.source_id = source_id
        self.after_post = after_post


def _as_built(result):
    if isinstance(result, Built):
        return result
    description, lines = result
    return Built(description, lines)


# ─── The operations ─────────────────────────────────────────────────────
def _build_capital(company_id, data, actor_id=None):
    """Dr cash/bank · Cr 3100 رأس المال."""
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    capital = _account_by_code(company_id, "3100", "رأس المال")
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


def _build_opening_balance(company_id, data, actor_id=None):
    """Dr cash/bank · Cr 3900 حساب الافتتاح."""
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    opening = _account_by_code(company_id, "3900", "حساب الافتتاح")
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


def _build_owner_drawings(company_id, data, actor_id=None):
    """Dr 3200 جاري الشركاء · Cr cash/bank — reduces equity."""
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    drawings = _account_by_code(company_id, "3200", "جاري الشركاء / المسحوبات")
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


# ─── Transfer between two money accounts ────────────────────────────────
# Cash did not increase or decrease here, it moved pots. Both legs land on
# cash accounts, so the entry nets to zero in cash_flow() and contributes
# nothing to the statement no matter which category it carries — NONCASH
# states that rather than leaning on it.
ACCRUED_EXPENSE_CODE = "2160"          # مصروفات مستحقة
ACCRUED_EXPENSE_KIND = "accrued_expense"


def _build_transfer(company_id, data, actor_id=None):
    """Dr destination · Cr source. Same money, different account."""
    amount = _amount(data)
    src, src_label = _money_account(company_id, data.get("account_id"))
    dst, dst_label = _money_account(company_id, data.get("account_id_to"))
    if src.id == dst.id:
        raise OperationError("لا يمكن التحويل من الحساب إلى نفسه")
    note = _note(data)
    desc = f"تحويل من {src_label} إلى {dst_label}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": dst.id, "debit": amount, "credit": 0,
         "memo": f"وارد من {src_label}"},
        {"account_id": src.id, "debit": 0, "credit": amount,
         "memo": f"تحويل إلى {dst_label}"},
    ]


# ─── The two-sided pair: accrue an expense, then pay it ─────────────────
def _build_accrue_expense(company_id, data, actor_id=None):
    """Dr expense · Cr 2160 — and open an item for what is now owed.

    The item is created and flushed here so the journal can carry its id
    as source_id; that link is what lets a reversal cancel it.
    """
    amount = _amount(data)
    expense, label = _account_under(company_id, "expense_account",
                                    data.get("expense_account_id"),
                                    "حساب المصروف")
    payable = _account_by_code(company_id, ACCRUED_EXPENSE_CODE,
                               "مصروفات مستحقة")
    note = _note(data)
    desc = f"إثبات مصروف مستحق — {label}"
    if note:
        desc += f" ({note})"

    from app.services.open_items import create_open_item
    item = create_open_item(
        company_id, ACCRUED_EXPENSE_KIND, payable.id, amount,
        description=desc, due_date=_optional_date(data, "due_date"),
        created_by=actor_id, note=note,
    )

    def _link(entry):
        item.journal_entry_id = entry.id

    return Built(desc, [
        {"account_id": expense.id, "debit": amount, "credit": 0,
         "memo": note or label},
        {"account_id": payable.id, "debit": 0, "credit": amount,
         "memo": "مصروف مستحق"},
    ], source_id=item.id, after_post=_link)


def _build_settle_accrued_expense(company_id, data, actor_id=None):
    """Dr 2160 · Cr cash/bank — pay down a specific open item.

    There is no free amount box: you settle an ITEM, and settle_open_item
    refuses more than the remainder and refuses a closed item. That is the
    whole point of §4 — otherwise the same accrual can be paid twice and
    the payable never clears.
    """
    from app.models import Account
    from app.services.open_items import resolve_open_item, settle_open_item
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    item = resolve_open_item(company_id, data.get("open_item_id"),
                             kind=ACCRUED_EXPENSE_KIND)
    payable = db.session.get(Account, item.account_id)
    note = _note(data)
    desc = f"سداد مصروف مستحق — {item.description or item.kind}"
    if note:
        desc += f" ({note})"

    leg = settle_open_item(item, amount, settled_on=_date(data),
                           created_by=actor_id)

    def _link(entry):
        leg.journal_entry_id = entry.id

    return Built(desc, [
        {"account_id": payable.id, "debit": amount, "credit": 0,
         "memo": "سداد مصروف مستحق"},
        {"account_id": money.id, "debit": 0, "credit": amount,
         "memo": f"صرف من {money_label}"},
    ], source_id=leg.id, after_post=_link)


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

    # ── MARSOUD-OPS-FOUNDATION (2026-08-05) ────────────────────────────
    Operation(
        key="transfer",
        title="تحويل بين الحسابات المالية",
        icon="🔁",
        description="نقل مبلغ من الصندوق إلى البنك أو بين بنكين. لا يغيّر إجمالي النقدية.",
        effect="ينقص الحساب المُحوَّل منه ويزيد الحساب المُحوَّل إليه بنفس المبلغ.",
        source_type="money_transfer",
        # Cash moved pots; it did not enter or leave the company. Left to
        # inference this would read as an operating inflow and add the
        # amount to the statement out of nowhere.
        cashflow_category="NONCASH",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ التحويل", "date", required=True),
            Field("account_id", "من حساب", "financial_account",
                  required=True, help_text="الحساب الذي خرجت منه الأموال"),
            Field("account_id_to", "إلى حساب", "financial_account_to",
                  required=True, help_text="الحساب الذي دخلت فيه الأموال"),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_transfer,
    ),
    Operation(
        key="accrue-expense",
        title="إثبات مصروف مستحق",
        icon="🧾",
        description="تسجيل مصروف تم استهلاكه ولم يُدفع بعد — إيجار أو كهرباء مستحقة.",
        effect="يزيد المصروف ويزيد الالتزام (2160 مصروفات مستحقة). لا نقدية تتحرك.",
        source_type="open_item",
        # No cash moves at all — the payment side is the settle operation.
        cashflow_category="NONCASH",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ الاستحقاق المحاسبي", "date", required=True),
            Field("expense_account_id", "حساب المصروف", "expense_account",
                  required=True,
                  help_text="نوع المصروف الذي تم استهلاكه"),
            Field("due_date", "تاريخ السداد المتوقع (اختياري)", "date"),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_accrue_expense,
        # §5 — an accrued expense owed to a NAMED supplier belongs in the
        # bills module, and one carrying input VAT belongs in purchases.
        # Both would post a journal that balances perfectly.
        forbids=("party", "tax"),
    ),
    Operation(
        key="settle-accrued-expense",
        title="سداد مصروف مستحق",
        icon="✅",
        description="دفع التزام سبق إثباته. تختار البند نفسه، لا تكتب مبلغًا حرًّا.",
        effect="ينقص الالتزام (2160) وينقص النقدية/البنك.",
        source_type="open_item_settle",
        # Paying an accrued operating expense is an operating outflow, and
        # this is the operation that proves the explicit category earns
        # its keep: left to inference the payable's code (2160) matches
        # `startswith("21") -> FINANCING`, so every such payment would be
        # reported as a financing outflow.
        cashflow_category="OPERATING",
        fields=[
            Field("open_item_id", "البند المراد سداده", "open_item",
                  required=True, item_kind=ACCRUED_EXPENSE_KIND,
                  help_text="تظهر البنود المفتوحة فقط، ويعرض كل بند المتبقي منه"),
            Field("amount", "المبلغ المدفوع", "amount", required=True,
                  help_text="لا يمكن تجاوز المتبقي من البند"),
            Field("date", "تاريخ السداد", "date", required=True),
            Field("account_id", "الحساب المالي", "financial_account",
                  required=True, help_text="الحساب الذي خرجت منه الأموال"),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_settle_accrued_expense,
        # Same boundary on the paying side: a payment to a named supplier
        # must drive that supplier's sub-account, not bare cash.
        forbids=("party", "tax"),
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
    # §5 — before anything is built or posted. Both of these produce a
    # journal that balances, so nothing downstream would object.
    enforce_boundaries(op, data)
    try:
        built = _as_built(op.build(company_id, data, actor_id))
        entry = post_journal(
            company_id=company_id,
            description=built.description,
            lines=built.lines,
            entry_date=entry_date,
            reference=op.key.upper(),
            created_by=actor_id,
            source_type=op.source_type,
            # Written now that builders can create rows: without it
            # _undo_source_side_effects has nothing to look up, and
            # reversing a settlement would leave the item settled.
            source_id=built.source_id,
            cashflow_category=op.cashflow_category,
        )
    except (LedgerError, OpenItemError) as e:
        # A builder may already have flushed a row. post_journal commits,
        # so anything flushed before it failed is still pending in the
        # session and the next unrelated commit would persist it.
        db.session.rollback()
        # Surface ledger complaints as the wizard's own error type so the
        # route only has to catch one thing.
        raise OperationError(str(e))
    except OperationError:
        db.session.rollback()
        raise

    if built.after_post:
        built.after_post(entry)
        db.session.commit()
    return entry
