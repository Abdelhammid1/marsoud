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
    """Block every non-superadmin from a route (403 instead of redirect).

    MARSOUD-APPROVAL-GATED-SUPERADMIN (2026-08-12) — when the
    caller is a superadmin with `requires_approval=True`, the
    request is routed through the approval gate. The gate returns
    None to let the call fall through (GET / self-scoped exempt /
    an in-flight approval replay), or a redirect/abort response to
    intercept it. All destructive superadmin.* write attempts
    become pending rows for the primary superadmin to decide
    from /admin/pending-actions.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(
            current_user, "is_superadmin", False
        ):
            abort(403)
        if getattr(current_user, "requires_approval", False):
            # Local import — the approval service imports models
            # that eventually import back through this module.
            from app.services.superadmin_approval import gate_request
            gated = gate_request()
            if gated is not None:
                return gated
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


def companies_with_stats(include_deleted=False):
    """Every company with a few cross-tenant counts. Used by /admin/companies.

    MARSOUD-BOT-PROTECTION-01 (Abdelhamid 2026-07-24) — also annotates
    each row with `owner_verified` (True iff the owner's status is
    not PENDING_VERIFICATION). The admin list uses this to hide bot
    signups by default until they verify their email.

    MARSOUD-COMPANIES-BULK-DELETE (2026-08-12) — soft-deleted rows
    (deleted_at IS NOT NULL) are hidden from the default query.
    /admin/companies/deleted passes include_deleted=True to see them.
    Callers that need EVERYTHING (both live + deleted, e.g. audit
    routes) can still pass include_deleted=True and filter downstream.
    """
    from app.models import UserStatus
    q = Company.query
    if not include_deleted:
        q = q.filter(Company.deleted_at.is_(None))
    rows = []
    for c in q.order_by(Company.created_at.desc()).all():
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
        # Owner verification status. If there are multiple owners, we
        # consider the company "verified" as soon as any one owner has
        # verified — the common case is one owner, and this handles
        # legacy multi-owner rows gracefully.
        owner_verified = db.session.execute(
            db.select(User.status)
            .join(user_companies,
                  user_companies.c.user_id == User.id)
            .where(user_companies.c.company_id == c.id)
            .where(user_companies.c.role == "owner")
        ).scalars().all()
        is_verified = any(
            s != UserStatus.PENDING_VERIFICATION.value
            for s in owner_verified
        ) if owner_verified else True
        # MARSOUD-TKT-ADMIN-OWNER-COL (2026-08-31) — resolve the primary
        # owner of the tenant company so /admin/companies can render a
        # clickable "owner" column linking to /admin/users/<id>. If a
        # company has more than one owner (rare, legacy), pick the
        # first one deterministically by user id.
        owner_row = (
            db.session.query(User)
            .join(user_companies, user_companies.c.user_id == User.id)
            .filter(user_companies.c.company_id == c.id)
            .filter(user_companies.c.role == "owner")
            .order_by(User.id.asc())
            .first()
        )
        rows.append({
            "company": c,
            "users": users,
            "journals": journals,
            "invoices": invoices,
            "last_activity": last_user_login,
            "owner_verified": is_verified,
            "owner": owner_row,   # User or None
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


def ai_usage_overview():
    """Per-company AI token usage vs plan quota (this month) plus
    all-time totals and a rough USD cost estimate. Used by
    /admin/ai-usage. MARSOUD-AI-USAGE-DASHBOARD (Abdelhamid 2026-07-29).
    """
    from app.models import Company, AiTokenUsage, QUOTA_AI_TOKENS_MONTH
    from app.services.quotas import (
        get_quota, is_unlimited, count_ai_tokens_this_month,
    )

    rows = []
    for c in Company.query.order_by(Company.created_at.desc()).all():
        month_used = count_ai_tokens_this_month(c)
        agg = db.session.query(
            func.coalesce(func.sum(AiTokenUsage.total_tokens), 0),
            func.coalesce(func.sum(AiTokenUsage.input_tokens), 0),
            func.coalesce(func.sum(AiTokenUsage.output_tokens), 0),
            func.count(AiTokenUsage.id),
            func.max(AiTokenUsage.created_at),
        ).filter(AiTokenUsage.company_id == c.id).first()
        all_time_total, all_time_input, all_time_output, call_count, last_used = agg

        if not all_time_total and not month_used:
            continue

        quota = get_quota(c, QUOTA_AI_TOKENS_MONTH)
        unlimited = is_unlimited(quota)
        included = int(quota.included_amount) if quota else None
        pct = round((month_used / included) * 100, 1) if (included and not unlimited) else None

        est_cost_usd = (int(all_time_input or 0) / 1_000_000 * 3.0
                        + int(all_time_output or 0) / 1_000_000 * 15.0)

        rows.append({
            "company": c,
            "month_used": month_used,
            "included": included,
            "unlimited": unlimited,
            "pct": pct,
            "all_time_total": int(all_time_total or 0),
            "all_time_input": int(all_time_input or 0),
            "all_time_output": int(all_time_output or 0),
            "call_count": int(call_count or 0),
            "last_used": last_used,
            "est_cost_usd": round(est_cost_usd, 4),
        })

    rows.sort(key=lambda r: r["month_used"], reverse=True)
    return rows


# ── MARSOUD-VBILL-STATUS-VISIBILITY (2026-08-17) — TKT-D ── #

def overdue_vendor_bills_by_company():
    """Cross-tenant super-admin view of every open, overdue vendor
    bill across all non-deleted companies.

    Returns a list of {company, bills:[{...bill dict}, ...],
    total_amount, currency}. Uses the SAME `vendor_bill_bucket`
    helper as the tenant dashboard + list so the super-admin never
    sees a different picture from the company it's mirroring.

    Skipped: deleted companies, tenants with zero overdue bills.
    Sorted most-overdue-total first so the busiest tenants surface
    at the top.
    """
    from datetime import date as _date
    from app.models import VendorBill, VendorBillStatus, Company
    from app.services.vendor_bills import vendor_bill_bucket

    today = _date.today()
    _OPEN = (VendorBillStatus.POSTED,
             VendorBillStatus.PARTIALLY_PAID,
             VendorBillStatus.OVERDUE,
             VendorBillStatus.PARTIALLY_REFUNDED)

    # One query. Group by company in Python — cheap for the number
    # of tenants Marsoud has (bill count matters more, and the
    # bucket check filters that down).
    raw = (VendorBill.query
           .join(Company, VendorBill.company_id == Company.id)
           .filter(Company.deleted_at.is_(None))
           .filter(VendorBill.deleted_at.is_(None))
           .filter(VendorBill.status.in_(_OPEN))
           .filter(VendorBill.due_date < today)
           .order_by(VendorBill.due_date.asc())
           .all())

    by_co = {}
    for b in raw:
        if vendor_bill_bucket(b, today=today) != "overdue":
            continue
        co = b.company
        entry = by_co.setdefault(co.id, {
            "company": co,
            "bills": [],
            "total_amount": 0.0,
        })
        amt = float(b.balance or 0)
        entry["total_amount"] += amt
        entry["bills"].append({
            "id": b.id,
            "number": b.number,
            "vendor_name": b.vendor.name if b.vendor else "—",
            "amount": amt,
            "currency": b.currency,
            "due_date": b.due_date,
            "days_late": (today - b.due_date).days
                         if b.due_date else 0,
        })

    rows = list(by_co.values())
    rows.sort(key=lambda r: r["total_amount"], reverse=True)
    return rows
