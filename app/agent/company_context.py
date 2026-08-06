"""MARSOUD-AGENT-CONTEXT-01 (2026-08-06) — build the per-turn context
block the accountant agent sees below its persona.

The old context (routes/agent.py:59-63) was 3 lines: name, currency,
VAT rate. The agent had no idea what today's date was in the
company's timezone (so "كام فاتورة النهاردة" returned yesterday's
data after midnight in Riyadh), which modules the plan enabled (so it
would happily try payroll operations for a company on the starter
plan), and which chart-of-accounts codes the company actually used
(so it fell back to the DEFAULT_COA codes hardcoded in the prompt
and gave wrong answers to any company that had edited its tree).

WHAT THIS BUILDER PRODUCES

  اسم الشركة: <name>
  العملة: <base_currency>
  نسبة الضريبة الافتراضية: <vat_rate>%

  📅 التاريخ
  اليوم: 2026-08-06 (الخميس)
  الشهر: أغسطس 2026
  بداية الشهر: 2026-08-01 · نهاية الشهر: 2026-08-31
  السنة المالية: 2026-01-01 → 2026-12-31

  📊 حجم الشركة
  عملاء نشطون: N · موردون نشطون: N · منتجات: N

  ⚙️ الوحدات المفعّلة في الباقة
  accounting · sales · crm · reports · settings

  📋 أهم الحسابات (من شجرة هذه الشركة)
  النقدية: 1110 - نقدي بالخزينة
  البنوك: 1121 - البنك الأهلي
  ...

TOKEN BUDGET

Every line here ships on every user turn — it sits below the cached
persona prefix, so it counts against every request. Keep it tight:
one line per account role (8 roles max), one line for modules, four
for date, one for size, one for name/currency/VAT. Measured against
a realistic tenant (~10 accounts, 50 customers, 30 vendors) it
lands ~450 tokens. Audit check 9 pins < 2000 chars.

CACHE ORDERING

This block goes BELOW the persona in run_agent_turn (base.py:51 does
the concatenation). The persona is the stable prefix Anthropic /
DeepSeek will cache; this block varies per tenant and MUST NOT be
inserted above the persona or the cache never hits.
"""
from datetime import date
from calendar import monthrange, month_name


# Month names in Arabic — indexed 1..12 for month().
_MONTHS_AR = (None, "يناير", "فبراير", "مارس", "أبريل", "مايو",
              "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر",
              "نوفمبر", "ديسمبر")

# Weekday names in Arabic — date.weekday() gives 0=Mon..6=Sun.
_WEEKDAYS_AR = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس",
                "الجمعة", "السبت", "الأحد")


# The eight account ROLES the agent needs by name. Each entry:
#   (label_ar, preferred_code, code_prefix, account_type_name)
# The preferred code is checked first (DEFAULT_COA seeded companies
# hit here). Failing that we search for a postable account whose
# code starts with the prefix and whose type matches — that catches
# tenants who renamed or moved the account but kept the numeric
# namespace, which is the realistic edit shape. Only if BOTH fail is
# the role rendered as "—  (غير مُعرَّف)" and the prompt's rule
# tells the agent to refuse the operation instead of guessing.
_ACCOUNT_ROLES = (
    ("النقدية",        "1110", "111", "ASSET"),
    ("البنوك",          "1121", "112", "ASSET"),
    ("العملاء (AR)",   "1130", "113", "ASSET"),
    ("الموردون (AP)",  "2110", "211", "LIABILITY"),
    ("المبيعات",        "4100", "41",  "REVENUE"),
    ("المشتريات",       "5100", "51",  "EXPENSE"),
    ("ضريبة المخرجات", "2120", "212", "LIABILITY"),
    ("ضريبة المدخلات", "1280", "128", "ASSET"),
)


def _resolve_role(company_id, preferred_code, code_prefix, type_name):
    """Return (code, name_ar) for one account role, or None-tuple if
    the company has neither the preferred code nor any postable
    account under the prefix + type. Read-only."""
    from app import db
    from app.models import Account, AccountType

    acc = Account.query.filter_by(
        company_id=company_id, code=preferred_code,
        is_active=True).first()
    if acc is not None:
        return acc.code, (acc.name_ar or acc.name or acc.code)

    # Fallback — first postable account matching prefix + type. Some
    # roles (AR / AP) legitimately point at a HEADER (1130) in the
    # seeded default; those cannot be posted to directly but the
    # AGENT is asking "which code represents customers" — the header
    # is the right answer. So we DON'T require is_postable here.
    try:
        t = AccountType[type_name]
    except KeyError:
        t = None
    q = Account.query.filter(
        Account.company_id == company_id,
        Account.code.like(f"{code_prefix}%"),
        Account.is_active.is_(True))
    if t is not None:
        q = q.filter(Account.type == t)
    acc = q.order_by(Account.code).first()
    if acc is not None:
        return acc.code, (acc.name_ar or acc.name or acc.code)
    return None, None


def _company_today(company):
    """today() in the company's timezone. Reuses services/time.py."""
    from app.services.time import today_in_company_tz
    return today_in_company_tz(company) if company else date.today()


def _date_block(today):
    """The date section — always uses the passed-in today so
    tests can freeze it."""
    m_ar = _MONTHS_AR[today.month]
    wd_ar = _WEEKDAYS_AR[today.weekday()]
    last_day = monthrange(today.year, today.month)[1]
    month_start = date(today.year, today.month, 1)
    month_end = date(today.year, today.month, last_day)
    # Fiscal year — Company.fiscal_start_month does not exist yet, so
    # calendar year. The one line to touch when it gets added.
    fy_start = date(today.year, 1, 1)
    fy_end = date(today.year, 12, 31)
    return (
        "📅 التاريخ\n"
        f"اليوم: {today.isoformat()} ({wd_ar})\n"
        f"الشهر: {m_ar} {today.year}\n"
        f"بداية الشهر: {month_start.isoformat()} · "
        f"نهاية الشهر: {month_end.isoformat()}\n"
        f"السنة المالية: {fy_start.isoformat()} → {fy_end.isoformat()}"
    )


def _size_block(company_id):
    """Active counts — three numbers, one line."""
    from app.models import Customer, Vendor, Product
    n_customers = Customer.query.filter_by(
        company_id=company_id, is_active=True).count()
    n_vendors = Vendor.query.filter_by(
        company_id=company_id, is_active=True).count()
    n_products = Product.query.filter_by(
        company_id=company_id, is_active=True).count()
    return (
        "📊 حجم الشركة\n"
        f"عملاء نشطون: {n_customers} · "
        f"موردون نشطون: {n_vendors} · "
        f"منتجات: {n_products}"
    )


def _modules_block(company):
    """One line, space-joined module codes from the company's plan.
    Missing plan renders "لا يوجد" — safer than a blank line.

    Company.plan is a LEGACY string column ("FREE"). The Plan model
    is reached via Company.subscription_plan (plan_id FK) — that's
    the relationship whose .modules property returns the enabled
    module codes."""
    plan = getattr(company, "subscription_plan", None)
    mods = list(plan.modules) if (plan is not None and plan.modules) else []
    label = " · ".join(mods) if mods else "لا يوجد"
    return (
        "⚙️ الوحدات المفعّلة في الباقة\n"
        f"{label}"
    )


def _accounts_block(company_id):
    """The eight-role account summary. Missing role → dash + honest
    "غير مُعرَّف" so the prompt's refusal rule fires instead of the
    agent guessing a code."""
    lines = ["📋 أهم الحسابات (من شجرة هذه الشركة)"]
    for label, pref, prefix, t in _ACCOUNT_ROLES:
        code, name = _resolve_role(company_id, pref, prefix, t)
        if code is None:
            lines.append(f"{label}: —  (غير مُعرَّف)")
        else:
            lines.append(f"{label}: {code} - {name}")
    return "\n".join(lines)


def build_company_context(company, *, today=None):
    """The full context block sent below the persona.

    Callable arg `today` is a hook for tests to freeze the date.
    Production passes None → resolves via company timezone.
    """
    if company is None:
        return ""
    day = today or _company_today(company)
    header = (
        f"اسم الشركة: {company.name}\n"
        f"العملة: {company.base_currency}\n"
        f"نسبة الضريبة الافتراضية: {company.vat_rate}%"
    )
    return "\n\n".join([
        header,
        _date_block(day),
        _size_block(company.id),
        _modules_block(company),
        _accounts_block(company.id),
    ])
