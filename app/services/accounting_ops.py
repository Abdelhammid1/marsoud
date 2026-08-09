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
from math import isfinite

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
    # MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08) — new pickers:
    # loan_kind          → 2-option select (قصير/طويل الأجل)
    # direction_variance → 2-option select (زيادة/عجز) — cash count
    # direction_dr_cr    → 2-option select (مدين/دائن) — general adjustment
    # any_account        → picker over EVERY postable account in the tree
    "loan_kind", "direction_variance", "direction_dr_cr", "any_account",
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
                 item_kind=None, party_type=None):
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
        # MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08, Phase 2) — party
        # picker filter: "customer" / "vendor" / "employee". None
        # keeps the pre-ticket behavior (all three types mixed).
        # Used by note-receivable (customer only), note-payable
        # (vendor only), bad-debt-writeoff (customer only), and
        # eosb-payment (employee only).
        self.party_type = party_type


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


# ─── Page groups (MARSOUD-OPS-FOUNDATION §6) ────────────────────────────
# The index was a flat wall of cards. Six is already hard to scan and the
# next wave makes it worse, so operations declare which family they belong
# to and the page renders the families in this order. A group with no
# operations the user may run is not rendered at all.
GROUPS = (
    ("cash",      "حركات نقدية"),
    ("expense",   "مصروفات وإيرادات"),
    ("accrual",   "استحقاقات وتسويات"),
    ("equity",    "حقوق الملكية"),
    ("debt",      "ديون وقروض"),
)
GROUP_KEYS = tuple(k for k, _ in GROUPS)
GROUP_LABELS = dict(GROUPS)


class Operation:
    def __init__(self, key, title, icon, description, source_type, fields,
                 build, cashflow_category, group, permission,
                 effect=None, forbids=()):
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

        # §6 — both REQUIRED and positional, for the same reason as the
        # cash-flow category: a new operation that forgets its permission
        # would silently inherit whatever the route happened to check.
        if group not in GROUP_KEYS:
            raise ValueError(
                f"operation {key!r}: group must be one of {GROUP_KEYS}, "
                f"got {group!r}")
        if not permission or not isinstance(permission, str):
            raise ValueError(
                f"operation {key!r}: permission is required — an operation "
                "without one cannot be gated at the route")
        self.group = group
        self.permission = permission


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


def party_choices(company_id, party_type=None):
    """[(group_label_ar, [(value, label), ...]), ...] for the picker.

    MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08, Phase 2) — optional
    `party_type` narrows the picker to one of "customer" / "vendor"
    / "employee". None keeps the pre-ticket behavior of mixing all
    three groups. Used by note-receivable (customer only), notes
    payable (vendor only), bad-debt write-off (customer only), and
    eosb-payment (employee only) so the picker never shows the
    wrong-shape counterparty for a given operation.
    """
    from app.models import Customer, Vendor
    from app.models.payroll import Employee
    if party_type is not None and party_type not in PARTY_TYPES:
        raise ValueError(
            f"unknown party_type {party_type!r} — expected one of "
            f"{PARTY_TYPES!r} or None")
    groups = []
    for ptype, model, label, order in (
        ("customer", Customer, "العملاء", "name"),
        ("vendor", Vendor, "الموردون", "name"),
        ("employee", Employee, "الموظفون", "name"),
    ):
        if party_type is not None and party_type != ptype:
            continue
        q = model.query.filter_by(company_id=company_id)
        if hasattr(model, "is_active"):
            q = q.filter(model.is_active.is_(True))
        rows = q.order_by(getattr(model, order)).all()
        if rows:
            groups.append((label, [(f"{ptype}:{r.id}", r.name) for r in rows]))
    return groups


def resolve_party(company_id, raw, expected_type=None):
    """'<type>:<id>' → (party, sub_account, label).

    Returns the party's OWN sub-account under 1130 / 2110 / 2130 — never
    the header. Every party operation in the system posts to that leaf,
    and the header is not postable anyway, so returning it would fail at
    save time with a confusing message.

    MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08, Phase 2) — optional
    `expected_type` refuses cross-type submissions at the service
    layer. If the wizard's field declared `party_type="customer"`,
    a crafted POST that submits `vendor:5` gets a clean rejection
    here (matching how `resolve_account_under` refuses accounts
    that weren't in the picker's offered set).
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
    if expected_type is not None and ptype != expected_type:
        raise OperationError("نوع الطرف غير مناسب لهذه العملية")
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
    # MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08) — every new picker
    # kind that renders as a grouped <select>. loan_kind / direction_*
    # are fixed-choice enums, any_account walks the whole tree.
    "loan_kind", "direction_variance", "direction_dr_cr", "any_account",
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
    # MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08) — the four new
    # picker kinds. Fixed-choice enums never emit "empty" here
    # (they always have both options); the fallback is for safety.
    "loan_kind": "لا توجد أنواع قروض متاحة — راجع الإعدادات.",
    "direction_variance": "لا يمكن اختيار جهة الفرق.",
    "direction_dr_cr": "لا يمكن اختيار جهة التسوية.",
    "any_account": "لا توجد حسابات قابلة للترحيل في شجرة الحسابات.",
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
            # MARSOUD-OPS-HUB-EXPANSION-01 (Phase 2) — pass through
            # the field's optional party_type so pickers can narrow.
            out[f.name] = party_choices(
                company_id, party_type=getattr(f, "party_type", None))
        elif f.kind == "open_item":
            from app.services.open_items import open_item_choices
            out[f.name] = open_item_choices(company_id, kind=f.item_kind)
        elif f.kind == "loan_kind":
            out[f.name] = [("نوع القرض", [
                ("short", "قصير الأجل (2140)"),
                ("long", "طويل الأجل (2210)"),
            ])]
        elif f.kind == "direction_variance":
            out[f.name] = [("نوع الفرق", [
                ("surplus", "زيادة — الصندوق أكبر من الدفاتر"),
                ("shortage", "عجز — الصندوق أقل من الدفاتر"),
            ])]
        elif f.kind == "direction_dr_cr":
            out[f.name] = [("جهة التسوية", [
                ("debit", "مدين"),
                ("credit", "دائن"),
            ])]
        elif f.kind == "any_account":
            # Every postable account in the tree. Grouped by first
            # digit (1/2/3/4/5) so the picker isn't a wall of codes.
            from app.models import Account
            rows = Account.query.filter_by(
                company_id=company_id, is_postable=True, is_active=True,
            ).order_by(Account.code).all()
            groups_map = {}
            for a in rows:
                head = (a.code or "?")[0]
                groups_map.setdefault(head, []).append(a)
            group_labels = {
                "1": "أصول", "2": "التزامات", "3": "حقوق ملكية",
                "4": "إيرادات", "5": "مصروفات",
            }
            out[f.name] = _accounts_as_choices([
                (group_labels.get(h, h), accounts)
                for h, accounts in sorted(groups_map.items())
            ])
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
    # float() accepts "nan" and "inf", and NEITHER is caught by the checks
    # below: `nan <= 0` is False, and so is every other comparison against
    # nan, so it sails through validation and reaches the database. The
    # column is NOT NULL, SQLite refuses the value, and the user gets a
    # 500 instead of a message. Measured on settle-accrued-expense with
    # amount=nan before this guard existed.
    if not isfinite(val):
        raise OperationError("المبلغ غير صالح")
    if val <= 0:
        raise OperationError("المبلغ يجب أن يكون أكبر من صفر")
    # A number this large is a typo or a probe, not a transaction, and it
    # would silently lose precision once stored as Numeric(15, 2).
    if val >= 10 ** 13:
        raise OperationError("المبلغ أكبر من الحد المسموح به")
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


# ─── MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08) ──────────────────────────
# Shared constants for the 18 new wizards. Kinds for the two new open-item
# families (accrued revenue + dividends) — free strings, no migration.
ACCRUED_REVENUE_KIND = "accrued_revenue"
DIVIDEND_KIND = "dividend"


# ─── Phase 3a: Loans (short/long receive + installment pay) ─────────────
def _build_receive_short_loan(company_id, data, actor_id=None):
    """Dr money-acc · Cr 2140 (short-term loans)."""
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    loan_acc = _account_by_code(company_id, "2140", "قروض قصيرة الأجل")
    note = _note(data)
    desc = f"استلام قرض قصير الأجل — {money_label}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": money.id, "debit": amount, "credit": 0,
         "memo": note or "قرض قصير الأجل"},
        {"account_id": loan_acc.id, "debit": 0, "credit": amount,
         "memo": "التزام قرض قصير الأجل"},
    ]


def _build_receive_long_loan(company_id, data, actor_id=None):
    """Dr money-acc · Cr 2210 (long-term loans)."""
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    loan_acc = _account_by_code(company_id, "2210", "قروض طويلة الأجل")
    note = _note(data)
    desc = f"استلام قرض طويل الأجل — {money_label}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": money.id, "debit": amount, "credit": 0,
         "memo": note or "قرض طويل الأجل"},
        {"account_id": loan_acc.id, "debit": 0, "credit": amount,
         "memo": "التزام قرض طويل الأجل"},
    ]


def _build_pay_loan_installment(company_id, data, actor_id=None):
    """Dr principal (2140/2210) + Dr 5940 (interest) · Cr money.

    Principal + interest go to different accounts; the loan header is
    picked from a two-choice select so we can validate against a
    known set rather than let the user type any account code."""
    # `amount` is the principal (matches the shared audit's
    # convention that every wizard has an "amount" field).
    # Interest is a separate optional field.
    principal_amount = _amount(data, name="amount")
    # Interest may legitimately be zero (grace-period installment),
    # so parse it manually without _amount's ">0" guard.
    try:
        interest_amount = round(
            float(data.get("interest_amount") or 0), 2)
    except (TypeError, ValueError):
        raise OperationError("مبلغ الفوائد غير صالح")
    from math import isfinite as _isfinite
    if not _isfinite(interest_amount) or interest_amount < 0:
        raise OperationError("مبلغ الفوائد غير صالح")
    money, money_label = _money_account(company_id, data.get("account_id"))
    loan_kind = (data.get("loan_kind") or "").strip()
    if loan_kind not in ("short", "long"):
        raise OperationError("اختر نوع القرض (قصير الأجل أو طويل الأجل)")
    principal_code = "2140" if loan_kind == "short" else "2210"
    principal_acc = _account_by_code(
        company_id, principal_code,
        "قروض قصيرة الأجل" if loan_kind == "short" else "قروض طويلة الأجل")
    interest_acc = _account_by_code(company_id, "5940",
                                     "فوائد وأعباء تمويلية")
    total = round(principal_amount + interest_amount, 2)
    if total <= 0:
        raise OperationError("لا يمكن سداد قسط بمبلغ صفر")
    note = _note(data)
    desc = f"سداد قسط قرض — {money_label}"
    if note:
        desc += f" ({note})"
    lines = [
        {"account_id": principal_acc.id, "debit": principal_amount,
         "credit": 0, "memo": "سداد أصل القرض"},
    ]
    if interest_amount > 0.005:
        lines.append({
            "account_id": interest_acc.id, "debit": interest_amount,
            "credit": 0, "memo": "فوائد وأعباء تمويلية",
        })
    lines.append({
        "account_id": money.id, "debit": 0, "credit": total,
        "memo": f"صرف من {money_label}",
    })
    return desc, lines


# ─── Phase 3b: VAT net payment ─────────────────────────────────────────
def _build_pay_vat_net(company_id, data, actor_id=None):
    """Dr 2120 (output-VAT balance) · Cr 1280 (input-VAT balance)
    with the difference to the money account.

    The wizard shows the computed net from vat_report() on the form;
    the user may confirm or cancel but cannot re-price it — the
    posted amount here is the same amount the report displays."""
    from app.models import JournalEntry, JournalLine
    from sqlalchemy import func
    from app import db as _db
    output_acc = _account_by_code(company_id, "2120", "ضريبة قيمة مضافة مخرجات")
    input_acc = _account_by_code(company_id, "1280", "ضريبة قيمة مضافة مدخلات")
    money, money_label = _money_account(company_id, data.get("account_id"))
    # Compute net from the ledger — sum of unreversed movements on
    # 2120 minus 1280 across ALL time (this wizard settles the full
    # accumulated balance). Simple, matches how a small business
    # actually files VAT.
    def _bal(acc):
        d, c = _db.session.query(
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        ).select_from(JournalLine).join(JournalEntry).filter(
            JournalLine.account_id == acc.id,
            JournalEntry.is_active.is_(True),
        ).first()
        return float(d or 0), float(c or 0)
    out_d, out_c = _bal(output_acc)
    in_d, in_c = _bal(input_acc)
    output_balance = round(out_c - out_d, 2)   # liability normal side = credit
    input_balance = round(in_d - in_c, 2)      # asset normal side = debit
    net = round(output_balance - input_balance, 2)
    if net <= 0.005:
        raise OperationError(
            f"لا يوجد صافي ضريبة مستحق (المخرجات {output_balance:.2f} — "
            f"المدخلات {input_balance:.2f}). لا حاجة للسداد.")
    note = _note(data)
    desc = f"سداد صافي ضريبة القيمة المضافة — {net:.2f}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": output_acc.id, "debit": output_balance,
         "credit": 0, "memo": "إقفال مخرجات الضريبة"},
        {"account_id": input_acc.id, "debit": 0, "credit": input_balance,
         "memo": "إقفال مدخلات الضريبة"},
        {"account_id": money.id, "debit": 0, "credit": net,
         "memo": f"صافي الضريبة إلى {money_label}"},
    ]


# ─── Phase 3c: Equity close ────────────────────────────────────────────
def _build_close_year_end(company_id, data, actor_id=None):
    """Dr 3400 (whole balance) · Cr 3300 — zero out current-year P&L
    into retained earnings. Amount is computed from the ledger, not
    user-editable — the whole point of a closing entry is to move
    the exact balance, not an arbitrary number."""
    from app.models import JournalEntry, JournalLine
    from sqlalchemy import func
    from app import db as _db
    pnl_acc = _account_by_code(company_id, "3400",
                                "أرباح/خسائر السنة الحالية")
    retained = _account_by_code(company_id, "3300", "أرباح مرحلة")
    d, c = _db.session.query(
        func.coalesce(func.sum(JournalLine.debit), 0),
        func.coalesce(func.sum(JournalLine.credit), 0),
    ).select_from(JournalLine).join(JournalEntry).filter(
        JournalLine.account_id == pnl_acc.id,
        JournalEntry.is_active.is_(True),
    ).first()
    balance = round(float(c or 0) - float(d or 0), 2)   # credit-side normal
    if abs(balance) < 0.005:
        raise OperationError(
            "رصيد 3400 صفر — لا يوجد ما يُقفَل هذه السنة")
    note = _note(data)
    desc = "إقفال نهاية السنة — تصفير 3400 في 3300"
    if note:
        desc += f" ({note})"
    if balance > 0:
        # net profit → Dr 3400 / Cr 3300
        return desc, [
            {"account_id": pnl_acc.id, "debit": balance, "credit": 0,
             "memo": "تصفير رصيد السنة الحالية"},
            {"account_id": retained.id, "debit": 0, "credit": balance,
             "memo": "ترحيل الأرباح إلى الأرباح المرحّلة"},
        ]
    # net loss → Dr 3300 / Cr 3400
    loss = -balance
    return desc, [
        {"account_id": retained.id, "debit": loss, "credit": 0,
         "memo": "تحميل الخسارة على الأرباح المرحّلة"},
        {"account_id": pnl_acc.id, "debit": 0, "credit": loss,
         "memo": "تصفير رصيد السنة الحالية"},
    ]


def _build_allocate_legal_reserve(company_id, data, actor_id=None):
    """Dr 3300 · Cr 3500 — carve out a chunk of retained earnings
    into the mandatory legal reserve. User picks the amount (usually
    10% of net profit up to a statutory ceiling)."""
    amount = _amount(data)
    retained = _account_by_code(company_id, "3300", "أرباح مرحلة")
    reserve = _account_by_code(company_id, "3500", "احتياطي قانوني")
    note = _note(data)
    desc = f"تخصيص احتياطي قانوني — {amount:.2f}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": retained.id, "debit": amount, "credit": 0,
         "memo": "خصم من الأرباح المرحّلة"},
        {"account_id": reserve.id, "debit": 0, "credit": amount,
         "memo": "احتياطي قانوني"},
    ]


# ─── Phase 3d: EOSB provision ──────────────────────────────────────────
def _build_provision_eosb(company_id, data, actor_id=None):
    """Dr expense (user picks) · Cr 2220 — build up the end-of-
    service benefits liability. Runs monthly or yearly at HR's
    discretion; the payment side is a separate Phase-4 wizard that
    resolves the specific employee."""
    amount = _amount(data)
    expense, label = _account_under(company_id, "expense_account",
                                     data.get("expense_account_id"),
                                     "حساب المصروف")
    provision = _account_by_code(company_id, "2220",
                                  "مخصص مكافأة نهاية الخدمة")
    note = _note(data)
    desc = f"تكوين مخصص مكافأة نهاية الخدمة — {label}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": expense.id, "debit": amount, "credit": 0,
         "memo": note or "مخصص مكافأة نهاية الخدمة"},
        {"account_id": provision.id, "debit": 0, "credit": amount,
         "memo": "مخصص EOSB"},
    ]


# ─── Phase 3e: Accrued revenue (record + collect) — mirror of accrued expense
def _build_accrue_revenue(company_id, data, actor_id=None):
    """Dr 1170 · Cr revenue_account — record income earned but
    not yet collected. Creates an open_item with kind='accrued_revenue'
    so the collect wizard can settle it later."""
    amount = _amount(data)
    revenue, label = _account_under(company_id, "revenue_account",
                                     data.get("revenue_account_id"),
                                     "حساب الإيراد")
    receivable = _account_by_code(company_id, "1170", "إيرادات مستحقة")
    note = _note(data)
    desc = f"إثبات إيراد مستحق — {label}"
    if note:
        desc += f" ({note})"
    from app.services.open_items import create_open_item
    item = create_open_item(
        company_id, ACCRUED_REVENUE_KIND, receivable.id, amount,
        description=desc, due_date=_optional_date(data, "due_date"),
        created_by=actor_id, note=note,
    )

    def _link(entry):
        item.journal_entry_id = entry.id

    return Built(desc, [
        {"account_id": receivable.id, "debit": amount, "credit": 0,
         "memo": "إيراد مستحق"},
        {"account_id": revenue.id, "debit": 0, "credit": amount,
         "memo": note or label},
    ], source_id=item.id, after_post=_link)


def _build_collect_accrued_revenue(company_id, data, actor_id=None):
    """Dr money-acc · Cr 1170 — collect a previously-recorded
    accrued-revenue item. Picker restricted to accrued_revenue
    kind so the user can't accidentally settle an accrued expense."""
    from app.models import Account
    from app.services.open_items import resolve_open_item, settle_open_item
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    item = resolve_open_item(company_id, data.get("open_item_id"),
                              kind=ACCRUED_REVENUE_KIND)
    receivable = db.session.get(Account, item.account_id)
    note = _note(data)
    desc = f"تحصيل إيراد مستحق — {item.description or item.kind}"
    if note:
        desc += f" ({note})"
    leg = settle_open_item(item, amount, settled_on=_date(data),
                            created_by=actor_id)

    def _link(entry):
        leg.journal_entry_id = entry.id

    return Built(desc, [
        {"account_id": money.id, "debit": amount, "credit": 0,
         "memo": f"وارد إلى {money_label}"},
        {"account_id": receivable.id, "debit": 0, "credit": amount,
         "memo": "تحصيل إيراد مستحق"},
    ], source_id=leg.id, after_post=_link)


# ─── Phase 3f: Dividends (declare + pay) ───────────────────────────────
def _build_declare_dividend(company_id, data, actor_id=None):
    """Dr 3300 (retained earnings) · Cr 2190 (dividends payable).
    Creates open_item(kind='dividend') so the pay wizard can settle."""
    amount = _amount(data)
    retained = _account_by_code(company_id, "3300", "أرباح مرحلة")
    payable = _account_by_code(company_id, "2190", "توزيعات مستحقة")
    note = _note(data)
    desc = f"إثبات توزيعات مستحقة — {amount:.2f}"
    if note:
        desc += f" ({note})"
    from app.services.open_items import create_open_item
    item = create_open_item(
        company_id, DIVIDEND_KIND, payable.id, amount,
        description=desc, due_date=_optional_date(data, "due_date"),
        created_by=actor_id, note=note,
    )

    def _link(entry):
        item.journal_entry_id = entry.id

    return Built(desc, [
        {"account_id": retained.id, "debit": amount, "credit": 0,
         "memo": "توزيعات من الأرباح المرحّلة"},
        {"account_id": payable.id, "debit": 0, "credit": amount,
         "memo": "توزيعات مستحقة"},
    ], source_id=item.id, after_post=_link)


def _build_pay_dividend(company_id, data, actor_id=None):
    """Dr 2190 · Cr money-acc — pay a declared dividend. Picker
    restricted to dividend kind."""
    from app.models import Account
    from app.services.open_items import resolve_open_item, settle_open_item
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    item = resolve_open_item(company_id, data.get("open_item_id"),
                              kind=DIVIDEND_KIND)
    payable = db.session.get(Account, item.account_id)
    note = _note(data)
    desc = f"سداد توزيعات — {item.description or item.kind}"
    if note:
        desc += f" ({note})"
    leg = settle_open_item(item, amount, settled_on=_date(data),
                            created_by=actor_id)

    def _link(entry):
        leg.journal_entry_id = entry.id

    return Built(desc, [
        {"account_id": payable.id, "debit": amount, "credit": 0,
         "memo": "سداد توزيعات"},
        {"account_id": money.id, "debit": 0, "credit": amount,
         "memo": f"صرف من {money_label}"},
    ], source_id=leg.id, after_post=_link)


# ─── Phase 3g: Deposits (receive + return) ─────────────────────────────
def _build_receive_deposit(company_id, data, actor_id=None):
    """Dr money-acc · Cr 2170 — take a deposit / retention from a
    party. No open_item — the return is at the discretion of the
    company (not a scheduled repayment)."""
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    deposit_acc = _account_by_code(company_id, "2170",
                                    "أمانات ومحتجزات")
    note = _note(data)
    desc = f"استلام أمانة — {money_label}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": money.id, "debit": amount, "credit": 0,
         "memo": note or "أمانة مستلمة"},
        {"account_id": deposit_acc.id, "debit": 0, "credit": amount,
         "memo": "أمانة مستلمة من طرف"},
    ]


def _build_return_deposit(company_id, data, actor_id=None):
    """Dr 2170 · Cr money-acc — return a deposit previously received."""
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    deposit_acc = _account_by_code(company_id, "2170",
                                    "أمانات ومحتجزات")
    note = _note(data)
    desc = f"رد أمانة — {money_label}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": deposit_acc.id, "debit": amount, "credit": 0,
         "memo": "رد أمانة لطرف"},
        {"account_id": money.id, "debit": 0, "credit": amount,
         "memo": f"صرف من {money_label}"},
    ]


# ─── Phase 4: party-scoped processors ──────────────────────────────────
def _build_receive_note_receivable(company_id, data, actor_id=None):
    """Dr 1140 (notes receivable) · Cr customer 1130-N sub."""
    amount = _amount(data)
    _, customer_acc, customer_label = resolve_party(
        company_id, data.get("party"), expected_type="customer")
    notes_receivable = _account_by_code(company_id, "1140",
                                         "أوراق قبض")
    note = _note(data)
    desc = f"استلام ورقة قبض من {customer_label}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": notes_receivable.id, "debit": amount, "credit": 0,
         "memo": f"ورقة قبض — {customer_label}"},
        {"account_id": customer_acc.id, "debit": 0, "credit": amount,
         "memo": "استبدال مديونية بورقة قبض"},
    ]


def _build_issue_note_payable(company_id, data, actor_id=None):
    """Dr vendor 2110-N sub · Cr 2115 (notes payable)."""
    amount = _amount(data)
    _, vendor_acc, vendor_label = resolve_party(
        company_id, data.get("party"), expected_type="vendor")
    notes_payable = _account_by_code(company_id, "2115",
                                      "أوراق دفع")
    note = _note(data)
    desc = f"إصدار ورقة دفع لـ{vendor_label}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": vendor_acc.id, "debit": amount, "credit": 0,
         "memo": "استبدال دين بورقة دفع"},
        {"account_id": notes_payable.id, "debit": 0, "credit": amount,
         "memo": f"ورقة دفع لـ{vendor_label}"},
    ]


def _build_writeoff_bad_debt(company_id, data, actor_id=None):
    """Dr 5910 (bad debt expense) · Cr customer 1130-N sub. Requires
    a note (why we gave up on this customer)."""
    amount = _amount(data)
    _, customer_acc, customer_label = resolve_party(
        company_id, data.get("party"), expected_type="customer")
    bad_debt = _account_by_code(company_id, "5910", "ديون معدومة")
    note = _note(data)
    if not note:
        raise OperationError("الرجاء توضيح سبب الشطب في الملاحظات")
    desc = f"شطب دين معدوم — {customer_label}"
    desc += f" ({note})"
    return desc, [
        {"account_id": bad_debt.id, "debit": amount, "credit": 0,
         "memo": f"شطب دين — {customer_label}"},
        {"account_id": customer_acc.id, "debit": 0, "credit": amount,
         "memo": note},
    ]


def _build_pay_eosb(company_id, data, actor_id=None):
    """Dr 2220 (EOSB provision) · Cr money-acc. Employee is picked
    to record who received the payment; the credit is still to the
    money account (EOSB is not a salary payable line)."""
    amount = _amount(data)
    money, money_label = _money_account(company_id, data.get("account_id"))
    _, _, employee_label = resolve_party(
        company_id, data.get("party"), expected_type="employee")
    provision = _account_by_code(company_id, "2220",
                                  "مخصص مكافأة نهاية الخدمة")
    note = _note(data)
    desc = f"صرف مكافأة نهاية الخدمة — {employee_label}"
    if note:
        desc += f" ({note})"
    return desc, [
        {"account_id": provision.id, "debit": amount, "credit": 0,
         "memo": f"صرف EOSB — {employee_label}"},
        {"account_id": money.id, "debit": 0, "credit": amount,
         "memo": f"صرف من {money_label}"},
    ]


def _build_cash_count_adjust(company_id, data, actor_id=None):
    """Dr 1110 · Cr 5960 (surplus) OR Dr 5960 · Cr 1110 (shortage).
    Restricted to 1110 (the physical cash box) — bank reconciliation
    is a separate feature."""
    amount = _amount(data)
    direction = (data.get("direction") or "").strip()
    if direction not in ("surplus", "shortage"):
        raise OperationError("اختر نوع الفرق: زيادة أم عجز")
    cash = _account_by_code(company_id, "1110", "النقدية / الصندوق")
    variance = _account_by_code(company_id, "5960", "فروق نقدية")
    note = _note(data)
    if not note:
        raise OperationError(
            "الرجاء توضيح سبب فرق الصندوق في الملاحظات")
    if direction == "surplus":
        desc = f"زيادة في الصندوق — {amount:.2f}"
        desc += f" ({note})"
        return desc, [
            {"account_id": cash.id, "debit": amount, "credit": 0,
             "memo": "زيادة صندوق"},
            {"account_id": variance.id, "debit": 0, "credit": amount,
             "memo": note},
        ]
    # shortage
    desc = f"عجز في الصندوق — {amount:.2f}"
    desc += f" ({note})"
    return desc, [
        {"account_id": variance.id, "debit": amount, "credit": 0,
         "memo": note},
        {"account_id": cash.id, "debit": 0, "credit": amount,
         "memo": "عجز صندوق"},
    ]


def _build_adjust_account(company_id, data, actor_id=None):
    """Dr/Cr picked account · Cr/Dr 5970 (misc adjustments). Note
    REQUIRED — this is the most dangerous wizard and any use of it
    without a reason is a red flag on audit. Any postable account
    can be adjusted; permission is behind ops.adjustments so
    accountant-role users don't have access by default."""
    from app.models import Account
    from app.services.ledger import postable_under
    amount = _amount(data)
    direction = (data.get("direction") or "").strip()
    if direction not in ("debit", "credit"):
        raise OperationError("اختر جهة التسوية: مدين أم دائن")
    account_id_raw = data.get("target_account_id")
    if not account_id_raw:
        raise OperationError("اختر الحساب المطلوب تسويته")
    try:
        account_id = int(account_id_raw)
    except (TypeError, ValueError):
        raise OperationError("الحساب المطلوب غير صالح")
    target = db.session.get(Account, account_id)
    if (not target or target.company_id != company_id
            or not target.is_postable):
        raise OperationError(
            "الحساب المطلوب غير صالح أو غير قابل للترحيل")
    misc = _account_by_code(company_id, "5970", "تسويات متنوعة")
    note = _note(data)
    if not note:
        raise OperationError(
            "السبب إلزامي في تسوية الحسابات — لا يمكن حفظ التسوية بدون توضيح")
    target_label = target.name_ar or target.name or target.code
    desc = f"تسوية حساب عام — {target_label} ({direction})"
    desc += f" — {note}"
    if direction == "debit":
        return desc, [
            {"account_id": target.id, "debit": amount, "credit": 0,
             "memo": note},
            {"account_id": misc.id, "debit": 0, "credit": amount,
             "memo": "تسويات متنوعة"},
        ]
    return desc, [
        {"account_id": misc.id, "debit": amount, "credit": 0,
         "memo": "تسويات متنوعة"},
        {"account_id": target.id, "debit": 0, "credit": amount,
         "memo": note},
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
        # The three original operations keep journals.create so nobody
        # loses access on deploy day.
        group="equity",
        permission="journals.create",
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
        group="equity",
        permission="journals.create",
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
        group="equity",
        permission="journals.create",
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
        # Moving money between the company's own accounts is a
        # narrower thing than posting arbitrary journals.
        group="cash",
        permission="ops.transfer",
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
        # Recording what is owed moves no money.
        group="accrual",
        permission="ops.accruals",
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
        # Settling DOES move money out, so it is a separate grant
        # from merely recording the obligation.
        group="accrual",
        permission="ops.settle",
        # Same boundary on the paying side: a payment to a named supplier
        # must drive that supplier's sub-account, not bare cash.
        forbids=("party", "tax"),
    ),

    # ── MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08) ─────────────────────
    # 18 new wizards. Grouped in the same order as the ticket for
    # easier cross-check: loans → VAT → equity → provisions →
    # accrued revenue → dividends → deposits → party-scoped → cash
    # count → dangerous general adjustment.

    # Phase 3a — Loans
    Operation(
        key="receive-short-loan",
        title="استلام قرض قصير الأجل",
        icon="💳",
        description="عند استلام قرض بنكي أو من طرف يُسدَّد خلال عام.",
        effect="يزيد النقدية/البنك ويزيد التزام قروض قصيرة الأجل (2140).",
        source_type="loan_short_receive",
        cashflow_category="FINANCING",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ الاستلام", "date", required=True),
            Field("account_id", "الحساب المالي المستلم", "financial_account",
                  required=True),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_receive_short_loan,
        group="debt",
        permission="journals.create",
    ),
    Operation(
        key="receive-long-loan",
        title="استلام قرض طويل الأجل",
        icon="🏦",
        description="عند استلام قرض يُسدَّد على أكثر من عام.",
        effect="يزيد النقدية/البنك ويزيد التزام قروض طويلة الأجل (2210).",
        source_type="loan_long_receive",
        cashflow_category="FINANCING",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ الاستلام", "date", required=True),
            Field("account_id", "الحساب المالي المستلم", "financial_account",
                  required=True),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_receive_long_loan,
        group="debt",
        permission="journals.create",
    ),
    Operation(
        key="pay-loan-installment",
        title="سداد قسط قرض",
        icon="📤",
        description="سداد قسط أصل + فوائد لقرض قصير أو طويل الأجل.",
        effect="ينقص أصل القرض ويزيد المصروفات التمويلية (5940) وينقص النقدية.",
        source_type="loan_installment_paid",
        cashflow_category="FINANCING",
        fields=[
            Field("loan_kind", "نوع القرض", "loan_kind", required=True,
                  help_text="قصير الأجل (2140) أو طويل الأجل (2210)"),
            Field("amount", "مبلغ الأصل", "amount", required=True),
            Field("interest_amount", "مبلغ الفوائد", "amount",
                  help_text="اترك صفر إذا كان القسط بدون فوائد"),
            Field("date", "تاريخ السداد", "date", required=True),
            Field("account_id", "الحساب المالي", "financial_account",
                  required=True),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_pay_loan_installment,
        group="debt",
        permission="journals.create",
    ),

    # Phase 3b — VAT
    Operation(
        key="pay-vat-net",
        title="سداد صافي ضريبة القيمة المضافة",
        icon="🧾",
        description="سداد الصافي المستحق للمصلحة عن الفترة الحالية.",
        effect="يقفل رصيد المخرجات (2120) مقابل المدخلات (1280)، والصافي يخرج من النقدية.",
        source_type="vat_net_payment",
        cashflow_category="OPERATING",
        fields=[
            Field("date", "تاريخ السداد", "date", required=True),
            Field("account_id", "الحساب المالي", "financial_account",
                  required=True),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_pay_vat_net,
        group="accrual",
        permission="journals.create",
    ),

    # Phase 3c — Equity close
    Operation(
        key="close-year-end",
        title="إقفال نهاية السنة",
        icon="📅",
        description="تصفير رصيد أرباح/خسائر السنة الحالية في الأرباح المرحّلة.",
        effect="ينقل رصيد 3400 (كامل) إلى 3300. المبلغ محسوب تلقائياً.",
        source_type="year_end_close",
        cashflow_category="NONCASH",
        fields=[
            Field("date", "تاريخ الإقفال", "date", required=True),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_close_year_end,
        group="equity",
        permission="journals.create",
    ),
    Operation(
        key="allocate-legal-reserve",
        title="تخصيص احتياطي قانوني",
        icon="🏛️",
        description="خصم جزء من الأرباح المرحّلة كاحتياطي قانوني.",
        effect="ينقص 3300 ويزيد الاحتياطي القانوني (3500).",
        source_type="legal_reserve_allocation",
        cashflow_category="NONCASH",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "التاريخ", "date", required=True),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_allocate_legal_reserve,
        group="equity",
        permission="journals.create",
    ),

    # Phase 3d — EOSB provision
    Operation(
        key="provision-eosb",
        title="تكوين مخصص مكافأة نهاية الخدمة",
        icon="🛡️",
        description="بناء مخصص EOSB شهرياً أو سنوياً. الصرف الفعلي يتم عند خروج الموظف.",
        effect="يزيد مصروف الرواتب (اختياره لك) ويزيد المخصص (2220).",
        source_type="eosb_provision",
        cashflow_category="NONCASH",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "التاريخ", "date", required=True),
            Field("expense_account_id", "حساب المصروف", "expense_account",
                  required=True,
                  help_text="عادةً حساب رواتب أو حساب مخصصات موظفين"),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_provision_eosb,
        group="accrual",
        permission="ops.accruals",
    ),

    # Phase 3e — Accrued revenue
    Operation(
        key="accrue-revenue",
        title="إثبات إيراد مستحق",
        icon="💵",
        description="تسجيل إيراد تم اكتسابه ولم يُحصَّل بعد.",
        effect="يزيد الإيرادات المستحقة (1170) ويزيد حساب الإيراد الذي تختاره.",
        source_type="open_item",
        cashflow_category="NONCASH",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ الاستحقاق المحاسبي", "date", required=True),
            Field("revenue_account_id", "حساب الإيراد", "revenue_account",
                  required=True),
            Field("due_date", "تاريخ التحصيل المتوقع (اختياري)", "date"),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_accrue_revenue,
        group="accrual",
        permission="ops.accruals",
    ),
    Operation(
        key="collect-accrued-revenue",
        title="تحصيل إيراد مستحق",
        icon="🪙",
        description="تحصيل بند إيراد سبق إثباته.",
        effect="يزيد النقدية وينقص الإيرادات المستحقة (1170).",
        source_type="open_item_settle",
        cashflow_category="OPERATING",
        fields=[
            Field("open_item_id", "البند المراد تحصيله", "open_item",
                  required=True, item_kind=ACCRUED_REVENUE_KIND,
                  help_text="تظهر البنود المفتوحة فقط"),
            Field("amount", "المبلغ المحصَّل", "amount", required=True,
                  help_text="لا يمكن تجاوز المتبقي من البند"),
            Field("date", "تاريخ التحصيل", "date", required=True),
            Field("account_id", "الحساب المالي", "financial_account",
                  required=True),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_collect_accrued_revenue,
        group="accrual",
        permission="ops.settle",
    ),

    # Phase 3f — Dividends
    Operation(
        key="declare-dividend",
        title="إثبات توزيعات مستحقة",
        icon="📊",
        description="إعلان توزيعات أرباح للمساهمين.",
        effect="ينقص الأرباح المرحّلة (3300) ويزيد التوزيعات المستحقة (2190).",
        source_type="open_item",
        cashflow_category="NONCASH",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ الإعلان", "date", required=True),
            Field("due_date", "تاريخ السداد المتوقع (اختياري)", "date"),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_declare_dividend,
        group="equity",
        permission="ops.accruals",
    ),
    Operation(
        key="pay-dividend",
        title="سداد توزيعات",
        icon="💸",
        description="صرف توزيعات سبق إعلانها.",
        effect="ينقص التوزيعات المستحقة (2190) وينقص النقدية.",
        source_type="open_item_settle",
        cashflow_category="FINANCING",
        fields=[
            Field("open_item_id", "التوزيعات المراد سدادها", "open_item",
                  required=True, item_kind=DIVIDEND_KIND),
            Field("amount", "المبلغ المدفوع", "amount", required=True,
                  help_text="لا يمكن تجاوز المتبقي"),
            Field("date", "تاريخ السداد", "date", required=True),
            Field("account_id", "الحساب المالي", "financial_account",
                  required=True),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_pay_dividend,
        group="equity",
        permission="ops.settle",
    ),

    # Phase 3g — Deposits
    Operation(
        key="receive-deposit",
        title="استلام أمانة من طرف",
        icon="🔐",
        description="أمانة أو محتجز من عميل أو مورد أو موظف.",
        effect="يزيد النقدية ويزيد أمانات ومحتجزات (2170).",
        source_type="deposit_received",
        cashflow_category="FINANCING",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ الاستلام", "date", required=True),
            Field("account_id", "الحساب المالي المستلم", "financial_account",
                  required=True),
            Field("notes", "من أي طرف ولأي غرض؟", "textarea"),
        ],
        build=_build_receive_deposit,
        group="cash",
        permission="journals.create",
    ),
    Operation(
        key="return-deposit",
        title="رد أمانة لطرف",
        icon="🔓",
        description="رد أمانة سبق استلامها.",
        effect="ينقص أمانات ومحتجزات (2170) وينقص النقدية.",
        source_type="deposit_returned",
        cashflow_category="FINANCING",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ الرد", "date", required=True),
            Field("account_id", "الحساب المالي", "financial_account",
                  required=True),
            Field("notes", "لأي طرف؟", "textarea"),
        ],
        build=_build_return_deposit,
        group="cash",
        permission="journals.create",
    ),

    # Phase 4 — party-scoped
    Operation(
        key="receive-note-receivable",
        title="استلام ورقة قبض من عميل",
        icon="📃",
        description="استبدال دين عميل بورقة قبض (كمبيالة أو شيك مؤجل).",
        effect="يزيد أوراق القبض (1140) وينقص رصيد العميل.",
        source_type="note_receivable_received",
        cashflow_category="NONCASH",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ الاستلام", "date", required=True),
            Field("party", "العميل", "party", required=True,
                  party_type="customer"),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_receive_note_receivable,
        group="accrual",
        permission="journals.create",
    ),
    Operation(
        key="issue-note-payable",
        title="إصدار ورقة دفع لمورد",
        icon="📝",
        description="استبدال دين مورد بورقة دفع.",
        effect="ينقص رصيد المورد ويزيد أوراق الدفع (2115).",
        source_type="note_payable_issued",
        cashflow_category="NONCASH",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ الإصدار", "date", required=True),
            Field("party", "المورد", "party", required=True,
                  party_type="vendor"),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_issue_note_payable,
        group="accrual",
        permission="journals.create",
    ),
    Operation(
        key="writeoff-bad-debt",
        title="شطب دين معدوم",
        icon="🗑️",
        description="شطب مديونية عميل تعذر تحصيلها. السبب إلزامي.",
        effect="يزيد مصروف الديون المعدومة (5910) وينقص رصيد العميل.",
        source_type="bad_debt_writeoff",
        cashflow_category="OPERATING",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "التاريخ", "date", required=True),
            Field("party", "العميل", "party", required=True,
                  party_type="customer"),
            Field("notes", "سبب الشطب (إلزامي)", "textarea", required=True),
        ],
        build=_build_writeoff_bad_debt,
        group="accrual",
        permission="ops.adjustments",
    ),
    Operation(
        key="pay-eosb",
        title="صرف مكافأة نهاية الخدمة",
        icon="🎁",
        description="صرف EOSB لموظف خارج من الشركة.",
        effect="ينقص مخصص EOSB (2220) وينقص النقدية.",
        source_type="eosb_payment",
        cashflow_category="OPERATING",
        fields=[
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ الصرف", "date", required=True),
            Field("party", "الموظف", "party", required=True,
                  party_type="employee"),
            Field("account_id", "الحساب المالي", "financial_account",
                  required=True),
            Field("notes", "ملاحظات (اختياري)", "textarea"),
        ],
        build=_build_pay_eosb,
        group="accrual",
        permission="ops.settle",
    ),
    Operation(
        key="cash-count-adjust",
        title="تسوية الصندوق (فرق نقدي)",
        icon="⚖️",
        description="تسوية زيادة أو عجز في الصندوق الفعلي مقابل الدفاتر. للصندوق (1110) فقط.",
        effect="يعدّل رصيد الصندوق ويقيّد الفرق على فروق نقدية (5960).",
        source_type="cash_count_adjustment",
        cashflow_category="NONCASH",
        fields=[
            Field("direction", "نوع الفرق", "direction_variance",
                  required=True,
                  help_text="زيادة (الصندوق فيه فلوس زيادة عن الدفاتر) أو عجز"),
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "تاريخ التسوية", "date", required=True),
            Field("notes", "سبب الفرق (إلزامي)", "textarea", required=True),
        ],
        build=_build_cash_count_adjust,
        group="cash",
        permission="ops.adjustments",
    ),
    Operation(
        key="adjust-account",
        title="تسوية حساب عام (خطير)",
        icon="⚠️",
        description="تعديل رصيد أي حساب لأي سبب. أخطر معالج في القائمة — يحتاج صلاحية مستقلة.",
        effect="يعدّل رصيد الحساب المختار ويقيّد الفرق على تسويات متنوعة (5970).",
        source_type="general_adjustment",
        cashflow_category="NONCASH",
        fields=[
            Field("target_account_id", "الحساب المطلوب تسويته", "any_account",
                  required=True,
                  help_text="أي حساب قابل للترحيل في الشجرة"),
            Field("direction", "جهة التسوية", "direction_dr_cr",
                  required=True,
                  help_text="مدين (يزيد الأصول ويقلل الالتزامات) أو دائن (العكس)"),
            Field("amount", "المبلغ", "amount", required=True),
            Field("date", "التاريخ", "date", required=True),
            Field("notes", "السبب (إلزامي)", "textarea", required=True),
        ],
        build=_build_adjust_account,
        group="accrual",
        permission="ops.adjustments",
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
