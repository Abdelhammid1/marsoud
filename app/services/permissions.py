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

    # ─── CRM (Leads) ───────────────────────────────────────────────────
    "leads.view":      {"owner", "admin", "ceo", "sales_manager", "sales_rep"},
    "leads.manage":    {"owner", "admin", "sales_manager", "sales_rep"},
    "leads.convert":   {"owner", "admin", "sales_manager", "sales_rep"},
    "leads.delete":    {"owner", "admin"},  # MARSOUD-47 — gated higher than .manage

    # ─── Projects ──────────────────────────────────────────────────────
    "projects.view":   {"owner", "admin", "ceo", "sales_manager", "sales_rep",
                        "project_manager", "team_member"},
    "projects.create": {"owner", "admin", "project_manager"},
    "projects.manage": {"owner", "admin", "project_manager"},

    # ─── Tasks ─────────────────────────────────────────────────────────
    "tasks.view":      {"owner", "admin", "ceo", "project_manager", "team_member"},
    "tasks.manage":    {"owner", "admin", "project_manager", "team_member"},

    "vendor_bills.create":  {"owner", "admin", "accountant"},
    "vendor_bills.delete":  {"owner", "admin"},  # MARSOUD-52 — DRAFT only, gated

    "accounts.manage":      {"owner", "admin", "accountant"},
    "partners.manage":      {"owner", "admin", "accountant"},   # customers + vendors
    "products.manage":      {"owner", "admin", "accountant"},
    "payment_methods.manage": {"owner", "admin", "accountant"},

    "assets.manage":        {"owner", "admin", "accountant"},

    "agent.use":            {"owner", "admin", "accountant"},   # agent can post journals → not viewer

    # ERP-01 — inventory + (Phase 2) POS
    "inventory.view":       {"owner", "admin", "accountant", "viewer"},
    "inventory.manage":     {"owner", "admin", "accountant"},
    # ERP-02 — POS register + void + reports.
    # NB: cashier role lives in a separate ticket but is already named here
    # so the perm wiring is ready when that role lands.
    "pos.use":              {"owner", "admin", "accountant", "cashier"},
    "pos.void":             {"owner", "admin"},
    "reports.profitability": {"owner", "admin", "accountant", "ceo"},
    "reports.cashier_sales": {"owner", "admin"},
    # ERP-03 — transfers + shifts
    "transfers.view":       {"owner", "admin", "accountant", "inventory_manager"},
    "transfers.manage":     {"owner", "admin", "accountant", "inventory_manager"},
    "shifts.manage":        {"owner", "admin", "cashier"},

    "reports.view":         {"owner", "admin", "accountant", "ceo", "viewer"},
    "reports.export":       {"owner", "admin", "accountant", "ceo", "viewer"},
}

ALL_ROLES = [
    "owner", "admin", "ceo", "accountant", "hr_manager",
    "sales_manager", "sales_rep",
    "project_manager", "team_member",
    "employee",
    "viewer", "client",
]
# All invitable for staff/clients except "owner" (per-company singleton).
# "employee" is also excluded — it's auto-provisioned by HR when an
# employee record is created, not picked from a manual invite dropdown.
INVITABLE_ROLES = [
    "admin", "ceo", "accountant", "hr_manager",
    "sales_manager", "sales_rep",
    "project_manager", "team_member",
    "viewer", "client",
]
ROLE_LABELS_AR = {
    "owner":           "مالك",
    "admin":           "مدير",
    "ceo":             "رئيس تنفيذي",
    "accountant":      "محاسب",
    "hr_manager":      "مدير الموارد البشرية",
    "sales_manager":   "مدير مبيعات",
    "sales_rep":       "مندوب مبيعات",
    "project_manager": "مدير مشروع",
    "team_member":     "عضو فريق",
    "employee":        "موظف (بوابة شخصية)",
    "viewer":          "مشاهد",
    "client":          "عميل (بوابة)",
}


def get_user_role(user_id, company_id):
    """Look up a user's role code (string) for a specific company.

    Returns the legacy string code so existing callers comparing against
    strings (e.g. role == "hr_manager") keep working. Returns None if
    there's no membership.
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


def get_user_role_id(user_id, company_id):
    """Look up the user's Role.id for the active company (MARSOUD-32).

    Returns None when the row hasn't been backfilled yet — callers fall
    back to the legacy string lookup.
    """
    from app import db
    from app.models.user import user_companies
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == user_id) &
            (user_companies.c.company_id == company_id)
        )
    ).first()
    return row.role_id if row else None


def _db_has_permission(action, user_id, company_id):
    """DB-backed permission check (MARSOUD-32). Returns None when
    indeterminate (e.g. role_id not backfilled) so the caller can fall
    back to the legacy P-dict lookup."""
    from app import db
    from app.models import Permission, role_permissions
    role_id = get_user_role_id(user_id, company_id)
    if role_id is None:
        return None
    granted = db.session.query(Permission.code).join(
        role_permissions, role_permissions.c.permission_id == Permission.id,
    ).filter(role_permissions.c.role_id == role_id).all()
    return action in {row[0] for row in granted}


def has_permission(action, user=None, company=None):
    user = user or current_user
    company = company or g.get("active_company")
    if not user or not getattr(user, "is_authenticated", False) or not company:
        return False

    # MARSOUD-32: prefer the DB; fall back to the legacy P dict if the
    # user_companies row hasn't been backfilled yet.
    try:
        db_result = _db_has_permission(action, user.id, company.id)
        if db_result is not None:
            return db_result
    except Exception:
        pass

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
