"""MARSOUD-SUPERADMIN-CONTROL-01 T6 (2026-08-08) — 360° composers.

Six pure read-only functions the admin/company_detail route calls
to build "everything about company X" on one screen. No writes,
no side-effects, no caching — every call is a fresh snapshot.

Each composer takes a Company and returns plain dicts / lists so
a future JSON export or dashboard tile can reuse them verbatim.

Order matches the template's card order:
  1. subscription_snapshot   → state + next billing + outstanding
  2. usage_snapshot          → four quota rows (current / included / pct)
  3. ai_usage_row            → aggregate tokens + cost + monthly cap
  4. owners_of               → list of owner Users
  5. module_matrix           → plan.modules ∩ kill-switches
  6. errors_preview          → latest N PlatformError rows

The route wraps each call in a try/except so one broken composer
never blanks the whole page (super-admins need this alive during
partial outages).
"""
from sqlalchemy import func

from app import db
from app.models import (
    User, PlatformError, AiTokenUsage,
    QUOTA_USERS, QUOTA_AI_TOKENS_MONTH, QUOTA_STORAGE_BYTES,
    QUOTA_BRANCHES,
)
from app.models.user import user_companies
from app.services.quotas import (
    get_quota, count_current, is_unlimited,
    count_ai_tokens_this_month,
)


KNOWN_QT = (QUOTA_USERS, QUOTA_AI_TOKENS_MONTH,
             QUOTA_STORAGE_BYTES, QUOTA_BRANCHES)

QT_LABELS_AR = {
    QUOTA_USERS: "المستخدمون",
    QUOTA_AI_TOKENS_MONTH: "توكنز الذكاء الاصطناعي (شهرياً)",
    QUOTA_STORAGE_BYTES: "المساحة التخزينية",
    QUOTA_BRANCHES: "الفروع",
}

# Superset of superadmin.py's MODULE_LABELS_AR — includes coarse
# modules that ship with the app but aren't in the plan-form list
# (evaluations / cash_custody / insights / settings). Fallback is
# the code itself.
MODULE_LABELS_AR = {
    "accounting": "المحاسبة",
    "sales": "المبيعات",
    "inventory": "المخزون",
    "purchases": "المشتريات",
    "pos": "نقطة البيع",
    "crm": "العملاء / CRM",
    "hr": "الموارد البشرية",
    "reports": "التقارير",
    "agent": "المحاسب الذكي",
    "manufacturing": "التصنيع",
    "employee_reports": "تقارير الموظفين اليومية",
    "evaluations": "تقييمات الموظفين",
    "cash_custody": "عهدة نقدية",
    "insights": "المحلل الذكي",
    "settings": "الإعدادات (دائماً مفعّل)",
}


def _pct_color(pct):
    if pct is None:
        return "gray"
    if pct >= 90:
        return "red"
    if pct >= 70:
        return "amber"
    return "green"


# ─── 1. Subscription snapshot ──────────────────────────────────────
def subscription_snapshot(company):
    """Compose the subscription-state card.

    Returns dict: state / days_remaining / banner / grace_end /
    read_only_enabled / next_billing_date / outstanding_saas_count /
    price_lock / frequency / expires_at.
    """
    from app.services.subscription import subscription_state
    from app.services.saas_billing import next_billing_date

    state = subscription_state(company)

    # Next-invoice: prefer the stored column (saas_billing keeps
    # it fresh); fall back to the computed value.
    nbd = getattr(company, "next_billing_date", None)
    if not nbd:
        try:
            nbd = next_billing_date(company)
        except Exception:
            nbd = None

    outstanding_count = _outstanding_saas_count(company)

    return {
        "state": state["state"],
        "days_remaining": state["days_remaining"],
        "banner": state["banner"],
        "grace_end": state["grace_end"],
        "read_only_enabled": state["read_only_enabled"],
        "next_billing_date": nbd,
        "outstanding_saas_count": outstanding_count,
        "price_lock": bool(getattr(company, "price_lock", False)),
        "frequency": getattr(company, "subscription_frequency", None),
        "expires_at": getattr(company, "subscription_expires_at", None),
    }


def _outstanding_saas_count(company):
    """Unpaid Manasty-side SaaS invoices for this tenant. Mirrors
    the filter in saas_billing.most_recent_unpaid_saas_invoice."""
    from app.models import Invoice, InvoiceStatus
    if not getattr(company, "saas_customer_id", None):
        return 0
    try:
        return int(
            db.session.query(func.count(Invoice.id))
            .filter(
                Invoice.customer_id == company.saas_customer_id,
                Invoice.source == "SAAS_BILLING",
                Invoice.status.in_([
                    InvoiceStatus.DRAFT, InvoiceStatus.SENT,
                    InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.OVERDUE,
                ]),
            ).scalar() or 0
        )
    except Exception:
        return 0


# ─── 2. Usage snapshot ─────────────────────────────────────────────
def usage_snapshot(company):
    """One row per KNOWN_QT — same shape as settings/usage.html.

    Returns list of dicts: quota_type / label_ar / current /
    included / pct / color / enforcement_mode / unlimited / unset.
    pct is None when unlimited or unset; unset=True marks quotas
    not configured on the plan.
    """
    cards = []
    for qt in KNOWN_QT:
        q = get_quota(company, qt)
        used = int(count_current(company, qt) or 0)
        if q is None:
            cards.append({
                "quota_type": qt,
                "label_ar": QT_LABELS_AR.get(qt, qt),
                "current": used,
                "included": None,
                "pct": None,
                "color": "gray",
                "enforcement_mode": None,
                "unlimited": False,
                "unset": True,
            })
            continue
        unlimited = is_unlimited(q)
        included = int(q.included_amount or 0)
        if unlimited:
            pct = None
        elif included <= 0:
            pct = 100.0 if used > 0 else 0.0
        else:
            pct = round((used / included) * 100, 1)
        cards.append({
            "quota_type": qt,
            "label_ar": QT_LABELS_AR.get(qt, qt),
            "current": used,
            "included": included,
            "pct": pct,
            "color": _pct_color(pct),
            "enforcement_mode": q.enforcement_mode,
            "unlimited": unlimited,
            "unset": False,
        })
    return cards


# ─── 3. AI usage row ───────────────────────────────────────────────
def ai_usage_row(company):
    """Same aggregate as ai_usage_overview() but for one company.

    Returns dict: input_tokens / output_tokens / total_tokens /
    total_calls / last_used_at / est_cost_usd / monthly_used /
    monthly_limit / monthly_unlimited.
    """
    agg = (db.session.query(
                func.coalesce(func.sum(AiTokenUsage.total_tokens), 0),
                func.coalesce(func.sum(AiTokenUsage.input_tokens), 0),
                func.coalesce(func.sum(AiTokenUsage.output_tokens), 0),
                func.count(AiTokenUsage.id),
                func.max(AiTokenUsage.created_at),
           ).filter(AiTokenUsage.company_id == company.id).first())
    all_time_total, all_time_input, all_time_output, call_count, last_used = agg

    month_used = int(count_ai_tokens_this_month(company) or 0)
    quota = get_quota(company, QUOTA_AI_TOKENS_MONTH)
    monthly_limit = int(quota.included_amount) if quota else None
    unlimited = is_unlimited(quota)

    est_cost_usd = (
        int(all_time_input or 0) / 1_000_000 * 3.0
        + int(all_time_output or 0) / 1_000_000 * 15.0
    )

    return {
        "input_tokens": int(all_time_input or 0),
        "output_tokens": int(all_time_output or 0),
        "total_tokens": int(all_time_total or 0),
        "total_calls": int(call_count or 0),
        "last_used_at": last_used,
        "est_cost_usd": round(est_cost_usd, 4),
        "monthly_used": month_used,
        "monthly_limit": monthly_limit,
        "monthly_unlimited": unlimited,
    }


# ─── 4. Owners ─────────────────────────────────────────────────────
def owners_of(company):
    """Users linked with role='owner' on this company. Empty list
    when there is no owner — the template surfaces a warning."""
    rows = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == company.id)
            & (user_companies.c.role == "owner"))
    ).fetchall()
    if not rows:
        return []
    ids = [r.user_id for r in rows]
    return (User.query.filter(User.id.in_(ids))
            .order_by(User.id).all())


# ─── 5. Module matrix ──────────────────────────────────────────────
def module_matrix(company):
    """One row per canonical module code, cross-referencing the
    company's plan.modules with the global kill-switch flags.

    effective = in_plan AND kill_switch_enabled. `settings` is
    always in_plan (it lives in plan_gating._ALWAYS_ALLOWED).
    """
    from app.services.plan_gating import (
        _PREFIX_TO_MODULE, _ALWAYS_ALLOWED,
    )
    from app.services.feature_flags import (
        is_module_enabled, disabled_reason,
    )
    codes = sorted(set(_PREFIX_TO_MODULE.values()))
    plan = getattr(company, "subscription_plan", None) \
        or getattr(company, "intended_plan", None)
    in_plan_set = set(plan.modules) if plan else set()
    out = []
    for code in codes:
        in_plan = (code in in_plan_set) or (code in _ALWAYS_ALLOWED)
        kill_enabled = is_module_enabled(code)
        effective = in_plan and kill_enabled
        out.append({
            "code": code,
            "label_ar": MODULE_LABELS_AR.get(code, code),
            "in_plan": in_plan,
            "kill_switch_enabled": kill_enabled,
            "effective": effective,
            "disabled_reason": disabled_reason(code) if not kill_enabled else None,
        })
    return out


# ─── 6. Errors preview ─────────────────────────────────────────────
def errors_preview(company, limit=10):
    """Latest N PlatformError rows for this company by created_at
    desc. Same query the errors_for_company page uses."""
    return (PlatformError.query
            .filter(PlatformError.company_id == company.id)
            .order_by(PlatformError.created_at.desc())
            .limit(int(limit))
            .all())
