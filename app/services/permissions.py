"""Per-company role-based permissions.

Roles (stored in user_companies.role):
  - owner       — full control, including managing users
  - admin       — full control except billing/user management
  - accountant  — can post entries (invoices, journals, payroll, vendor bills)
                  but cannot edit company settings or manage users
  - hr_manager  — sees people (employees/departments + read-only payroll), never numbers
                  (no journals/invoices/vendor_bills/accounts/reports)
  - viewer      — read-only

Use @require_permission("invoices.create") on routes that mutate data.
Read-only routes only need @login_required.
"""
from functools import wraps
from flask import g, flash, redirect, url_for, abort
from flask_login import current_user

# Role → permission set
P = {
    "users.manage":         {"owner"},
    "users.view":           {"owner", "admin"},

    "company.edit":         {"owner", "admin"},
    "company.create":       {"owner", "admin"},

    "invoices.create":      {"owner", "admin", "accountant"},
    "invoices.send":        {"owner", "admin", "accountant"},
    "invoices.refund":      {"owner", "admin", "accountant"},

    "journals.create":      {"owner", "admin", "accountant"},
    "journals.pause":       {"owner", "admin", "accountant"},
    "journals.reverse":     {"owner", "admin", "accountant"},
    "journals.recurring":   {"owner", "admin", "accountant"},

    "payroll.run":          {"owner", "admin", "accountant"},
    "payroll.employees":    {"owner", "admin", "accountant", "hr_manager"},  # employee lifecycle (create/edit/terminate) — HR owns this
    "payroll.accruals":     {"owner", "admin", "accountant"},  # settling accruals = posts a journal → financial-only
    "payroll.view":         {"owner", "admin", "accountant", "hr_manager", "viewer"},

    "hr.manage":            {"owner", "admin", "hr_manager"},  # departments + employee HR fields

    "vendor_bills.create":  {"owner", "admin", "accountant"},

    "accounts.manage":      {"owner", "admin", "accountant"},
    "partners.manage":      {"owner", "admin", "accountant"},   # customers + vendors
    "products.manage":      {"owner", "admin", "accountant"},
    "payment_methods.manage": {"owner", "admin", "accountant"},

    "assets.manage":        {"owner", "admin", "accountant"},

    "agent.use":            {"owner", "admin", "accountant"},   # agent can post journals → not viewer

    "reports.view":         {"owner", "admin", "accountant", "viewer"},
    "reports.export":       {"owner", "admin", "accountant", "viewer"},
}

ALL_ROLES = ["owner", "admin", "accountant", "hr_manager", "viewer"]
INVITABLE_ROLES = ["admin", "accountant", "hr_manager", "viewer"]  # not "owner" — that's per-company singleton
ROLE_LABELS_AR = {
    "owner": "مالك",
    "admin": "مدير",
    "accountant": "محاسب",
    "hr_manager": "مدير الموارد البشرية",
    "viewer": "مشاهد",
}


def get_user_role(user_id, company_id):
    """Look up a user's role for a specific company.

    Reads user_companies association table directly. Returns None if there's
    no membership.
    """
    from app import db
    from app.models.user import user_companies
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == user_id) &
            (user_companies.c.company_id == company_id)
        )
    ).first()
    return row.role if row else None


def has_permission(action, user=None, company=None):
    user = user or current_user
    company = company or g.get("active_company")
    if not user or not getattr(user, "is_authenticated", False) or not company:
        return False
    role = get_user_role(user.id, company.id)
    if not role:
        return False
    return role in P.get(action, set())


def require_permission(action):
    """Decorator: enforce permission for a route. Flashes + redirects on denial."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login"))
            if not g.get("active_company"):
                flash("اختر شركة أولاً", "warning")
                return redirect(url_for("dashboard.index"))
            if not has_permission(action):
                flash("ليس لديك صلاحية لهذا الإجراء", "error")
                return redirect(url_for("dashboard.index"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def hr_required(fn):
    """Decorator for HR routes — allows OWNER / ADMIN / HR_MANAGER, 403 for others.

    Per HR-04 spec: "HR_MANAGER يحصل على 403 عند محاولة الوصول للفواتير …".
    The reverse (everyone except the HR-allowed roles → 403 on HR routes) is
    the same rule.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        if not g.get("active_company"):
            flash("اختر شركة أولاً", "warning")
            return redirect(url_for("dashboard.index"))
        role = get_user_role(current_user.id, g.active_company.id)
        if role not in ("owner", "admin", "hr_manager"):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def forbid_roles(*roles):
    """Decorator: returns 403 (not redirect) for users whose role matches any in `roles`.

    Used on financial routes to satisfy the spec acceptance #18 — HR_MANAGER
    must get a real 403 when poking at /journals, /invoices, /reports, etc.
    Other roles fall through to the existing require_permission gate (which
    handles viewer redirects, etc.).
    """
    blocked = set(roles)

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if current_user.is_authenticated and g.get("active_company"):
                role = get_user_role(current_user.id, g.active_company.id)
                if role in blocked:
                    abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ─── Invitation token helpers ────────────────────────────────────────────
def _serializer():
    from itsdangerous import URLSafeTimedSerializer
    from flask import current_app
    secret = current_app.config.get("SECRET_KEY")
    return URLSafeTimedSerializer(secret, salt="marsoud-invite")


def generate_invite_token(payload):
    return _serializer().dumps(payload)


def parse_invite_token(token, max_age_seconds=7 * 24 * 3600):
    try:
        return _serializer().loads(token, max_age=max_age_seconds)
    except Exception:
        return None
