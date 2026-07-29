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

    # MARSOUD-REFUNDS-01 — refunds page + report (read-only view). Manage
    # implies the ability to actually issue a refund on both sides
    # (sales via invoices.refund, purchases via vendor_bills.refund).
    "refunds.view":         {"owner", "admin", "accountant", "ceo", "viewer"},
    "refunds.manage":       {"owner", "admin", "accountant"},
    "vendor_bills.refund":  {"owner", "admin", "accountant"},

    # MARSOUD-EMPLOYEE-DAILY-REPORTS — owner + any admin/manager can be
    # granted view rights. Actual per-employee filtering is enforced by
    # employee_report_access (checked in daily_digest.can_view_reports_for).
    "employee_reports.view": {"owner", "admin", "hr_manager"},

    # MARSOUD-MANUFACTURING-01 — new coarse module. .complete is
    # separated from .manage because posting a work order (which pulls
    # inventory + posts a journal) is more sensitive than editing a BOM.
    "manufacturing.view":     {"owner", "admin", "accountant", "ceo", "viewer"},
    "manufacturing.manage":   {"owner", "admin", "accountant"},
    "manufacturing.complete": {"owner", "admin", "accountant"},

    "journals.create":      {"owner", "admin", "accountant"},
    "journals.pause":       {"owner", "admin", "accountant"},
    "journals.reverse":     {"owner", "admin", "accountant"},
    "journals.recurring":   {"owner", "admin", "accountant"},
    # MARSOUD — read access to the general ledger. Was missing entirely,
    # which left /journals/* routes wide-open to any logged-in user. Same
    # role list as the financial-reports gate (+ accountant explicitly).
    "journals.view":        {"owner", "admin", "accountant", "ceo", "viewer"},

    "payroll.run":          {"owner", "admin", "accountant"},
    "payroll.employees":    {"owner", "admin", "accountant", "hr_manager"},  # employee lifecycle (create/edit/terminate) — HR owns this
    "payroll.accruals":     {"owner", "admin", "accountant"},  # settling accruals = posts a journal → financial-only
    "payroll.view":         {"owner", "admin", "accountant", "hr_manager", "viewer"},
    # MARSOUD-PERM-FIX-01 — view employee personal/HR data WITHOUT salary
    # figures. Whoever has payroll.view sees the salary numbers; this is the
    # broader gate for the employees list + profile (data without numbers).
    "employees.view":       {"owner", "admin", "accountant", "hr_manager",
                             "sales_manager", "ceo", "project_manager", "viewer"},

    "hr.manage":            {"owner", "admin", "hr_manager"},  # departments + employee HR fields

    # MARSOUD-EVALUATIONS-PRO-GATING (Batch 5 Ticket 6, 2026-07-29) —
    # employee-evaluations (cycles, targets, actuals, bonuses). HR
    # owns the operational side; owner + admin control everything.
    # Sales/PM/finance roles have no business in HR performance reviews.
    "evaluations.manage":   {"owner", "admin", "hr_manager"},

    # ─── CRM (Leads) ───────────────────────────────────────────────────
    "leads.view":      {"owner", "admin", "ceo", "sales_manager", "sales_rep"},
    # MARSOUD-PERM-FIX-01 — view ALL company leads (not just assigned-to-me).
    # Without this, leads.view shows only the user's own assigned leads.
    "leads.view_all":  {"owner", "admin", "ceo", "sales_manager"},
    "leads.manage":    {"owner", "admin", "sales_manager", "sales_rep"},
    "leads.convert":   {"owner", "admin", "sales_manager", "sales_rep"},
    "leads.delete":    {"owner", "admin"},  # MARSOUD-47 — gated higher than .manage

    # ─── Projects ──────────────────────────────────────────────────────
    "projects.view":   {"owner", "admin", "ceo", "sales_manager", "sales_rep",
                        "project_manager", "team_member"},
    # MARSOUD-PERM-FIX (PM scope) — see ALL company projects (not just
    # the ones you manage or are a member of).
    "projects.view_all": {"owner", "admin", "ceo"},
    "projects.create": {"owner", "admin", "project_manager"},
    "projects.manage": {"owner", "admin", "project_manager"},

    # ─── Tasks ─────────────────────────────────────────────────────────
    # ASMAA-FIX 2026-07-03 — broadened .view + .manage to every
    # business-user role. Tasks aren't a financial action, and any
    # user in the company should be able to log meetings/reminders/
    # todos for themselves. Delete + archive stay owner/admin.
    "tasks.view":      {"owner", "admin", "ceo", "project_manager",
                        "team_member", "sales_manager", "sales_rep",
                        "hr_manager", "accountant"},
    # MARSOUD-PERM-FIX-01 — see ALL tasks in the company (not just assigned).
    "tasks.view_all":  {"owner", "admin"},
    "tasks.manage":    {"owner", "admin", "project_manager",
                        "team_member", "sales_manager", "sales_rep",
                        "hr_manager", "accountant", "ceo"},
    # MARSOUD-PERM-FIX (PM scope) — hard delete is owner/admin only.
    # Project managers + team members can edit their assigned tasks via
    # tasks.manage, but cannot delete them.
    "tasks.delete":    {"owner", "admin"},
    # MARSOUD-TASK-ARCHIVE-01 — soft-archive completed tasks. Owner/admin
    # only; non-destructive but should still be a managerial action.
    "tasks.archive":   {"owner", "admin"},

    # MARSOUD-PERM-FIX (PM scope) — customers module is sales-facing.
    # Project managers + team members are programming-focused and don't
    # need (and shouldn't see) customer data. partners.manage stays the
    # write-side gate; customers.view is the new read-side gate.
    "customers.view":  {"owner", "admin", "accountant", "ceo",
                        "sales_manager", "sales_rep", "viewer"},

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

    # ─── MARSOUD-PERM-EXPAND — per-endpoint permissions for new sidebar
    # items. Defaults deliberately mirror the umbrella permission (whose
    # grants stay unchanged), and each new code is IMPLIED by its
    # umbrella below so existing roles keep working without a data
    # migration. Owners who want fine-grained control can now toggle
    # them individually from the Roles page.

    # CRM sub-items (default = same roles as leads.view)
    "crm.campaigns.view":   {"owner", "admin", "ceo", "sales_manager", "sales_rep"},
    "crm.activities.view":  {"owner", "admin", "ceo", "sales_manager", "sales_rep"},
    "crm.contacts.view":    {"owner", "admin", "ceo", "sales_manager", "sales_rep"},
    "crm.analytics.view":   {"owner", "admin", "ceo", "sales_manager"},

    # Party ledger (default = same roles as reports.view)
    "party_ledger.view":    {"owner", "admin", "accountant", "ceo", "viewer"},

    # Owner-level settings pages (default = same roles as users.manage)
    "api_tokens.manage":    {"owner", "admin"},
    "activity_log.view":    {"owner", "admin"},
    "backup.download":      {"owner", "admin"},

    # MARSOUD-SIDEBAR-COMPLETE (Abdelhamid 2026-07-24) — two orphan
    # pages had no permission code, so the sidebar couldn't gate them
    # per role. Both are owner-scoped: /settings/usage shows the
    # company's quota consumption + billable overage, and
    # /companies/ lists every company this user owns / co-owns.
    "settings_usage.view":  {"owner"},
    "companies.manage":     {"owner"},

    # MARSOUD-SUPPORT-TICKETS-01 (Abdelhamid 2026-07-24) — narrow
    # cross-tenant permission granted only inside Manasty. Wired via
    # the new `support_agent` role seeded on Manasty's company id.
    # Owner is included so Abdelhamid never needs to grant it to
    # himself before opening /support-admin/ the first time.
    "support.manage_tickets": {"owner", "support_agent"},
}

ALL_ROLES = [
    "owner", "admin", "ceo", "accountant", "hr_manager",
    "sales_manager", "sales_rep",
    "project_manager", "team_member",
    "employee",
    "viewer", "client",
    # MARSOUD-SUPPORT-TICKETS-01 — Manasty-only role.
    "support_agent",
]
# All invitable for staff/clients except "owner" (per-company singleton).
# "employee" is also excluded — it's auto-provisioned by HR when an
# employee record is created, not picked from a manual invite dropdown.
INVITABLE_ROLES = [
    "admin", "ceo", "accountant", "hr_manager",
    "sales_manager", "sales_rep",
    "project_manager", "team_member",
    "viewer", "client",
    # MARSOUD-SUPPORT-TICKETS-01 — invitable INSIDE Manasty only.
    # The support decorator layer verifies Manasty membership so
    # granting this role in a customer company is a no-op.
    "support_agent",
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
    "support_agent":   "موظف دعم فني (منصتي)",
}


def _is_company_member(user_id, company_id):
    """True if there's an active user_companies row. Used by the
    tasks bypass so any company member can view + create tasks."""
    from app import db
    from app.models.user import user_companies
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == user_id) &
            (user_companies.c.company_id == company_id)
        )
    ).first()
    return row is not None


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


# MARSOUD-PERM-FIX-01 — *.view_all implies *.view at the route gate.
# Without this, an owner who grants only leads.view_all (intending
# "manager sees ALL leads") would still get redirected because the route
# decorator asks for leads.view. The visibility helpers inside the
# routes layer on top of this to widen the result set.
_IMPLIES = {
    "leads.view": "leads.view_all",
    "tasks.view": "tasks.view_all",
    # MARSOUD — anyone who can create a journal entry can read the ledger.
    # Lets us add the journals.view gate without locking accountants out.
    "journals.view": "journals.create",
    # MARSOUD-PERM-FIX (PM scope) — projects.view_all implies projects.view,
    # and partners.manage (the write-side gate) implies customers.view so
    # accountants editing customers don't need a second checkbox.
    "projects.view": "projects.view_all",
    "customers.view": "partners.manage",
    # MARSOUD-PERM-EXPAND — new per-endpoint permissions inherit from
    # their umbrella so existing roles keep working before any re-seed.
    # e.g. anyone with leads.view automatically has crm.campaigns.view.
    "crm.campaigns.view":  "leads.view",
    "crm.activities.view": "leads.view",
    "crm.contacts.view":   "leads.view",
    "crm.analytics.view":  "leads.view",
    "party_ledger.view":   "reports.view",
    "api_tokens.manage":   "users.manage",
    "activity_log.view":   "users.manage",
    "backup.download":     "users.manage",
    # MARSOUD-REFUNDS-01 — anyone who could already issue a sales refund
    # can see the new /refunds page. Vendor-side refund (manage) inherits
    # from the same gate so no re-seed of the roles table is needed.
    "refunds.view":        "invoices.refund",
    "refunds.manage":      "invoices.refund",
    "vendor_bills.refund": "invoices.refund",
}


def _db_has_permission(action, user_id, company_id):
    """DB-backed permission check (MARSOUD-32). Returns None when
    indeterminate (e.g. role_id not backfilled) so the caller can fall
    back to the legacy P-dict lookup."""
    from app import db
    from app.models import Permission, role_permissions
    role_id = get_user_role_id(user_id, company_id)
    if role_id is None:
        return None
    granted = {row[0] for row in db.session.query(Permission.code).join(
        role_permissions, role_permissions.c.permission_id == Permission.id,
    ).filter(role_permissions.c.role_id == role_id).all()}
    if action in granted:
        return True
    implier = _IMPLIES.get(action)
    if implier and implier in granted:
        return True
    return False


def has_permission(action, user=None, company=None):
    user = user or current_user
    company = company or g.get("active_company")
    if not user or not getattr(user, "is_authenticated", False) or not company:
        return False

    # ASMAA-FIX 2026-07-03 (round 2). Ibrahim's principle: "if she can
    # be assigned a task, she must be able to see + create tasks."
    # Every authenticated user with an active membership in the
    # current company gets tasks.view + tasks.manage regardless of
    # role or plan. The earlier P-dict + resync-system-roles fix
    # missed her because:
    #   1. plan_gating (below) runs before the role check — if her
    #      company's plan doesn't include the "crm" module, no role
    #      unlocks tasks.
    #   2. resync-system-roles ONLY touches system roles by design.
    #      If Asmaa is on a CUSTOM cloned role, her role_permissions
    #      row is untouched.
    # This bypass short-circuits both. tasks.delete / .archive /
    # .view_all stay on the standard role check — destructive or
    # cross-user actions still need explicit grant.
    if action in ("tasks.view", "tasks.manage"):
        if _is_company_member(user.id, company.id):
            return True

    # MARSOUD-57.2 — plan gating runs BEFORE the role check. If the
    # company's plan doesn't include the action's module, no role can
    # bypass that. Super-admins are exempt (they need to admin every
    # company regardless of plan).
    if not getattr(user, "is_superadmin", False):
        try:
            from app.services.plan_gating import plan_allows
            if not plan_allows(action, company):
                return False
        except Exception:
            pass

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
    if role in P.get(action, set()):
        return True
    # Same implication as the DB-backed path: leads.view_all → leads.view,
    # tasks.view_all → tasks.view.
    implier = _IMPLIES.get(action)
    if implier and role in P.get(implier, set()):
        return True
    return False


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
def _serializer(salt="marsoud-invite"):
    """MARSOUD-EMAIL-VERIFY / MARSOUD-PW-RESET — accept a custom salt
    so verify + reset flows produce distinct token spaces. An invite
    token can never be replayed as a verify or reset token (or vice
    versa) even if SECRET_KEY leaks partial state."""
    from itsdangerous import URLSafeTimedSerializer
    from flask import current_app
    secret = current_app.config.get("SECRET_KEY")
    return URLSafeTimedSerializer(secret, salt=salt)


def generate_invite_token(payload):
    return _serializer("marsoud-invite").dumps(payload)


def parse_invite_token(token, max_age_seconds=7 * 24 * 3600):
    try:
        return _serializer("marsoud-invite").loads(token, max_age=max_age_seconds)
    except Exception:
        return None


# MARSOUD-EMAIL-VERIFY — one-shot token for the verify-email link.
def generate_verify_email_token(user_id):
    return _serializer("marsoud-verify-email").dumps({"user_id": int(user_id)})


def parse_verify_email_token(token, max_age_seconds=7 * 24 * 3600):
    try:
        return _serializer("marsoud-verify-email").loads(
            token, max_age=max_age_seconds)
    except Exception:
        return None


# MARSOUD-LOCKOUT-RESET — password-reset token. Includes the last 12
# chars of the password_hash so the token is invalidated the moment
# the password changes (a leaked token from an old email can't be
# reused after the user resets).
def generate_password_reset_token(user):
    return _serializer("marsoud-password-reset").dumps({
        "user_id": int(user.id),
        "h": (user.password_hash or "")[-12:],
    })


def parse_password_reset_token(token, max_age_seconds=3600):
    """1-hour expiry by default. Returns the payload dict or None."""
    try:
        return _serializer("marsoud-password-reset").loads(
            token, max_age=max_age_seconds)
    except Exception:
        return None
