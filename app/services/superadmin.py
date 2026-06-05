"""Super-admin layer — global-scope helpers, decorator, and audit logger.

Everything here intentionally bypasses the per-company tenant filters used by
the rest of the app. Routes that pull from these helpers MUST be guarded by
@superadmin_required.
"""
from datetime import datetime, timedelta
from functools import wraps
from flask import abort, request, session
from flask_login import current_user
from sqlalchemy import func
from app import db
from app.models import (
    User, Company, JournalEntry, Invoice, PlatformAuditLog,
    SuperadminImpersonation, user_companies,
)


def superadmin_required(fn):
    """Block every non-superadmin from a route (403 instead of redirect)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(
            current_user, "is_superadmin", False
        ):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


# ── audit logging ─────────────────────────────────────────────────────────── #
def log_platform_action(action, *, target_company_id=None, target_user_id=None,
                        actor_id=None, details=None):
    """Write one row to platform_audit_logs. Safe to call without a request."""
    ip = None
    try:
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr)
    except RuntimeError:
        pass  # outside request context
    if actor_id is None and current_user.is_authenticated:
        actor_id = current_user.id
    entry = PlatformAuditLog(
        actor_id=actor_id,
        action=action,
        target_company_id=target_company_id,
        target_user_id=target_user_id,
        ip_address=ip,
        details=details,
    )
    db.session.add(entry)
    db.session.commit()
    return entry


# ── platform metrics ──────────────────────────────────────────────────────── #
def platform_overview():
    now = datetime.utcnow()
    h24 = now - timedelta(hours=24)
    d7 = now - timedelta(days=7)

    total_companies = Company.query.count()
    active_companies = Company.query.filter(Company.status == "ACTIVE").count()
    suspended_companies = Company.query.filter(Company.status == "SUSPENDED").count()
    trial_companies = Company.query.filter(Company.status == "TRIAL").count()

    total_users = User.query.count()
    active_24h = User.query.filter(User.last_login_at.isnot(None),
                                   User.last_login_at >= h24).count()
    active_7d = User.query.filter(User.last_login_at.isnot(None),
                                  User.last_login_at >= d7).count()

    total_journals = JournalEntry.query.count()
    total_invoices = Invoice.query.count()

    last_logins = (User.query
                   .filter(User.last_login_at.isnot(None))
                   .order_by(User.last_login_at.desc())
                   .limit(20).all())

    recent_audit = (PlatformAuditLog.query
                    .order_by(PlatformAuditLog.created_at.desc())
                    .limit(15).all())

    return {
        "total_companies": total_companies,
        "active_companies": active_companies,
        "suspended_companies": suspended_companies,
        "trial_companies": trial_companies,
        "total_users": total_users,
        "active_24h": active_24h,
        "active_7d": active_7d,
        "total_journals": total_journals,
        "total_invoices": total_invoices,
        "last_logins": last_logins,
        "recent_audit": recent_audit,
    }


def companies_with_stats():
    """Every company with a few cross-tenant counts. Used by /admin/companies."""
    rows = []
    for c in Company.query.order_by(Company.created_at.desc()).all():
        users = db.session.query(func.count()).select_from(user_companies).filter(
            user_companies.c.company_id == c.id
        ).scalar() or 0
        journals = JournalEntry.query.filter_by(company_id=c.id).count()
        invoices = Invoice.query.filter_by(company_id=c.id).count()
        last_user_login = (
            db.session.query(func.max(User.last_login_at))
            .join(user_companies, user_companies.c.user_id == User.id)
            .filter(user_companies.c.company_id == c.id)
            .scalar()
        )
        rows.append({
            "company": c,
            "users": users,
            "journals": journals,
            "invoices": invoices,
            "last_activity": last_user_login,
        })
    return rows


def users_with_companies():
    """Every user across the platform with their company memberships."""
    return User.query.order_by(User.created_at.desc()).all()


# ── impersonation / view-as ───────────────────────────────────────────────── #
IMPERSONATION_SESSION_KEY = "superadmin_viewing_as_company_id"
IMPERSONATION_RECORD_KEY = "superadmin_impersonation_id"


def start_impersonation(company_id, reason=None):
    """Begin a read-only view-as session against the given company."""
    ip = request.remote_addr if request else None
    rec = SuperadminImpersonation(
        superadmin_id=current_user.id,
        company_id=company_id,
        ip_address=ip,
        reason=reason,
    )
    db.session.add(rec)
    db.session.commit()
    session[IMPERSONATION_SESSION_KEY] = company_id
    session[IMPERSONATION_RECORD_KEY] = rec.id
    log_platform_action("impersonation_start",
                        target_company_id=company_id,
                        details=reason)
    return rec


def end_impersonation():
    rec_id = session.pop(IMPERSONATION_RECORD_KEY, None)
    company_id = session.pop(IMPERSONATION_SESSION_KEY, None)
    if rec_id:
        rec = db.session.get(SuperadminImpersonation, rec_id)
        if rec and not rec.ended_at:
            rec.ended_at = datetime.utcnow()
            db.session.commit()
    if company_id:
        log_platform_action("impersonation_end",
                            target_company_id=company_id)


def is_impersonating():
    return bool(session.get(IMPERSONATION_SESSION_KEY))
