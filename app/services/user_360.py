"""MARSOUD-SUPERADMIN-USER-360 (2026-08-17) — one-shot snapshot for
the super-admin User Detail page (/admin/users/<id>).

Six sub-dicts (one per tab):
  · basic          basic account fields + status + last_login + counts
  · companies      one row per user_companies membership
  · roles          per-company role code + granted permissions
  · activity       recent UserActivityLog rows (paginated inline, 100 max)
  · login_history  recent UserSession rows (100 max)
  · invitations    Invitation rows tied to this user's email

Every list is oldest-newest-truncated to keep the render cheap. The
route can page beyond that via ?page= if needed later.

`plan_snapshot(company)` from MARSOUD-PLAN-SSOT is reused for the
per-company plan chip so the User-360° page shows the same plan/status
labels as the Company-360° page and /api/v1/me.
"""
from sqlalchemy import desc

from app import db
from app.models import (
    User, Company, Invitation, user_companies,
)
from app.models.activity import UserActivityLog, UserSession
from app.models.consent import ConsentEvent
from app.models.roles import Role


_ROLE_LABEL_AR = {
    "owner":       "مالك",
    "admin":       "مدير",
    "accountant":  "محاسب",
    "sales":       "مبيعات",
    "hr_manager":  "مدير موارد بشرية",
    "hr":          "موارد بشرية",
    "employee":    "موظف",
    "client":      "عميل بوابة",
    "manager":     "مدير قسم",
}


def _role_label(code):
    if not code:
        return "—"
    if code in _ROLE_LABEL_AR:
        return _ROLE_LABEL_AR[code]
    if code.startswith("custom_"):
        return "دور مخصّص"
    return code


def user_snapshot(user_id, *, limit_activity=100, limit_sessions=50,
                  limit_invitations=50, limit_consent=50):
    """Assemble the full User 360° snapshot.

    Returns None when the user doesn't exist — the route hands that
    back as 404 without a further DB probe.
    """
    from app.services.plan_snapshot import plan_snapshot

    user = db.session.get(User, user_id)
    if user is None:
        return None

    # ── companies + per-company role/plan snapshot ──────────────────
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.user_id == user_id)
    ).all()

    companies = []
    for row in rows:
        co = db.session.get(Company, row.company_id)
        if co is None:
            continue
        role_code = row.role
        role_obj = None
        if row.role_id:
            role_obj = db.session.get(Role, row.role_id)
        try:
            plan_ss = plan_snapshot(co)
        except Exception:
            plan_ss = None
        companies.append({
            "company":       co,
            "role_code":     role_code,
            "role_label_ar": _role_label(role_code),
            "role_obj":      role_obj,
            "role_id":       row.role_id,
            "plan_snapshot": plan_ss,
        })

    # ── roles + granted permissions per company ─────────────────────
    roles_per_company = []
    for c in companies:
        perms = []
        if c["role_obj"] is not None:
            perms = sorted(
                {p.code for p in c["role_obj"].permissions}
            )
        roles_per_company.append({
            "company":       c["company"],
            "role_code":     c["role_code"],
            "role_label_ar": c["role_label_ar"],
            "role_obj":      c["role_obj"],
            "permissions":   perms,
            "permission_count": len(perms),
            "source":        "role" if c["role_obj"] else "legacy_string",
        })

    # ── activity log ────────────────────────────────────────────────
    activity = (UserActivityLog.query
                .filter_by(user_id=user_id)
                .order_by(desc(UserActivityLog.created_at))
                .limit(limit_activity)
                .all())

    # ── login/session history ───────────────────────────────────────
    sessions = (UserSession.query
                .filter_by(user_id=user_id)
                .order_by(desc(UserSession.login_at))
                .limit(limit_sessions)
                .all())
    active_sessions = sum(1 for s in sessions if s.status == "ACTIVE")

    # ── invitations sent TO this user (matched by email) + BY user ──
    invitations_received = (Invitation.query
                            .filter(Invitation.email == user.email)
                            .order_by(desc(Invitation.created_at))
                            .limit(limit_invitations)
                            .all())
    invitations_sent = (Invitation.query
                        .filter(Invitation.invited_by_id == user_id)
                        .order_by(desc(Invitation.created_at))
                        .limit(limit_invitations)
                        .all())

    # ── consent events (legal audit trail) ──────────────────────────
    consent = (ConsentEvent.query
               .filter_by(user_id=user_id)
               .order_by(desc(ConsentEvent.created_at))
               .limit(limit_consent)
               .all())

    # ── login counts ────────────────────────────────────────────────
    login_count = UserActivityLog.query.filter_by(
        user_id=user_id, action_type="LOGIN").count()

    return {
        "user":                 user,
        "companies":            companies,
        "company_count":        len(companies),
        "roles_per_company":    roles_per_company,
        "activity":             activity,
        "activity_shown":       len(activity),
        "sessions":             sessions,
        "session_count_shown":  len(sessions),
        "active_sessions":      active_sessions,
        "invitations_received": invitations_received,
        "invitations_sent":     invitations_sent,
        "consent_events":       consent,
        "login_count":          login_count,
    }
