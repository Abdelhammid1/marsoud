"""MARSOUD-SUPERADMIN-CONTROL-01 T10 (2026-08-08) — Ctrl+K palette.

Compose grouped search results for the admin palette overlay.
Pure read helpers — no writes, no cache, no side-effects.

Three groups fill the palette:
  التنقّل     — nav catalog matches (up to 8)
  الشركات    — company by name / subdomain / id (up to 6)
  المستخدمون — user by email / full_name (up to 5)

Every result is a dict {label, icon, url, kind, hint?}
so the JS in admin/base.html renders them uniformly.

The nav catalog lives here (not derived from admin/base.html)
because the palette needs stable endpoint metadata Python can
search against. Two sources of truth is OK for something whose
only failure mode is "a nav item missing from the palette".
"""
from app import db


# (endpoint, label_ar, icon, section_key)
NAV_CATALOG = [
    ("superadmin.dashboard",             "النظرة العامة",              "📊", "tenants"),
    ("superadmin.companies",             "الشركات",                    "🏢", "tenants"),
    ("superadmin.companies_inactive",    "الشركات غير النشطة",        "🏚️", "tenants"),
    ("superadmin.users",                 "المستخدمون",                 "👥", "tenants"),
    ("superadmin.impersonations",        "سجل المعاينات",             "👁️", "tenants"),

    ("superadmin.plans_index",           "الباقات",                    "📦", "billing"),
    ("superadmin.coupons_index",         "أكواد الخصم",               "🎟️", "billing"),
    ("superadmin.subscriptions_index",   "الاشتراكات",                 "⏰", "billing"),
    ("superadmin.saas_index",            "فوترة SaaS",                 "💳", "billing"),
    ("superadmin.subscription_settings", "إعدادات الاشتراك",          "⚙️", "billing"),

    ("superadmin.feature_flags_index",   "مفاتيح الميزات",            "🚩", "features_ai"),
    ("superadmin.ai_usage",              "استهلاك الذكاء الاصطناعي",  "🤖", "features_ai"),
    ("superadmin.ai_settings",           "إعدادات الذكاء الاصطناعي",  "🧠", "features_ai"),

    ("superadmin.broadcasts_index",      "الإشعارات الجماعية",         "📢", "content"),
    ("superadmin.consent_index",         "سجل الموافقات",              "📋", "content"),
    ("superadmin.legal",                 "المستندات القانونية",        "⚖️", "content"),
    ("superadmin.help_index",            "مركز المساعدة",              "❓", "content"),

    ("superadmin.audit",                 "سجل النشاط",                 "📜", "logs"),
    ("superadmin.errors_global",         "سجل الأخطاء",                "🛑", "logs"),

    ("superadmin.email_test",            "اختبار الإيميل",            "📨", "tools"),
]

SECTION_LABELS_AR = {
    "tenants":     "الشركات والمستخدمون",
    "billing":     "الاشتراكات والفوترة",
    "features_ai": "الميزات والذكاء الاصطناعي",
    "content":     "الإشعارات والمحتوى",
    "logs":        "المراقبة والسجلات",
    "tools":       "أدوات",
}


def _match(q, s):
    """Case-insensitive substring match — safe on Arabic and
    Latin. Both q and s are strings; empty q matches every s."""
    if not q:
        return True
    return q.lower() in (s or "").lower()


def nav_results(q, limit=8):
    """Nav items filtered by q. Empty q returns the whole catalog
    (up to limit). Matches label OR the endpoint's local name."""
    from flask import url_for
    out = []
    for (endpoint, label, icon, _section) in NAV_CATALOG:
        local = endpoint.split(".", 1)[-1]
        if _match(q, label) or _match(q, local):
            try:
                url = url_for(endpoint)
            except Exception:
                # Blueprint not registered in this test run — skip.
                continue
            out.append({"label": label, "icon": icon,
                         "url": url, "kind": "nav",
                         "endpoint": endpoint})
        if len(out) >= limit:
            break
    return out


def company_results(q, limit=6):
    """Top N companies matching q by name / subdomain / id. Empty
    q returns []. Skips soft-deleted rows."""
    from flask import url_for
    from app.models import Company
    if not q:
        return []
    like = f"%{q}%"
    filters = [Company.name.ilike(like), Company.subdomain.ilike(like)]
    if q.isdigit():
        filters.append(Company.id == int(q))
    query = (Company.query
             .filter(Company.deleted_at.is_(None))
             .filter(db.or_(*filters)))
    return [{"label": c.name,
             "hint": (c.subdomain or "").lower(),
             "icon": "🏢",
             "url": url_for("superadmin.company_detail",
                            company_id=c.id),
             "kind": "company"}
            for c in query.limit(limit).all()]


def user_results(q, limit=5):
    """Top N users matching q by email / full_name."""
    from flask import url_for
    from app.models import User
    if not q:
        return []
    like = f"%{q}%"
    query = (User.query
             .filter(db.or_(
                 User.email.ilike(like),
                 User.full_name.ilike(like)))
             .filter_by(is_active=True))
    return [{"label": u.full_name or u.email,
             "hint": u.email,
             "icon": "👤",
             "url": url_for("superadmin.users"),
             "kind": "user"}
            for u in query.limit(limit).all()]


def search_all(q, *, nav_limit=8, company_limit=6, user_limit=5):
    """Compose grouped results the palette JS renders. Only
    non-empty groups appear."""
    groups = []
    nav = nav_results(q, limit=nav_limit)
    if nav:
        groups.append({"key": "nav", "title": "التنقّل",
                        "items": nav})
    comps = company_results(q, limit=company_limit)
    if comps:
        groups.append({"key": "companies", "title": "الشركات",
                        "items": comps})
    users = user_results(q, limit=user_limit)
    if users:
        groups.append({"key": "users", "title": "المستخدمون",
                        "items": users})
    return {"q": q, "groups": groups}
