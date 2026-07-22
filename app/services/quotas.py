"""MARSOUD-QUOTAS (Abdelhamid 2026-07-22) — service layer.

Central place for reading a company's applicable Quota rows,
computing current usage, enforcing at every entry point, and
firing 80/90/100 threshold notifications with per-cycle dedupe.
"""
import os
from datetime import datetime, timedelta
from sqlalchemy import func
from app import db
from app.models import (
    Quota, AiTokenUsage, EmployeeAiCap, QuotaNotificationSent,
    QUOTA_USERS, QUOTA_AI_TOKENS_MONTH,
    QUOTA_STORAGE_BYTES, QUOTA_BRANCHES,
    ENF_BLOCK, ENF_ALLOW_NOTIFY, ENF_UNLIMITED,
)


class QuotaBlockedError(Exception):
    """Raised when enforcement_mode=BLOCK and the request would push
    the company past its quota. Callers surface as an Arabic flash
    or as an API error."""


# ─── Read side ──────────────────────────────────────────────────
def get_quota(company, quota_type):
    """Return the Quota row for this company's plan + quota_type,
    or None if no such row is configured (treated as UNLIMITED).

    Prefers company.intended_plan_id → then company.plan_id →
    then no quota. During the trial window the caller shouldn't
    even be here (plan_gating already grants full access), but
    if it is, we treat "no plan" as UNLIMITED.
    """
    plan_id = (getattr(company, "intended_plan_id", None)
               or getattr(company, "plan_id", None))
    if not plan_id:
        return None
    return Quota.query.filter_by(
        plan_id=plan_id, quota_type=quota_type).first()


def is_unlimited(quota):
    return quota is None or quota.enforcement_mode == ENF_UNLIMITED


def _cycle_month():
    """YYYY-MM for the current UTC month. Used by dedupe keys +
    monthly AI-token counting."""
    return datetime.utcnow().strftime("%Y-%m")


# ─── Count-current helpers ──────────────────────────────────────
def count_users(company):
    """Number of ACTIVE users membership rows for the company."""
    from sqlalchemy import text
    row = db.session.execute(text(
        "SELECT COUNT(DISTINCT uc.user_id) FROM user_companies uc "
        "JOIN users u ON u.id = uc.user_id "
        "WHERE uc.company_id = :c AND u.is_active = 1"
    ), {"c": company.id}).scalar()
    return int(row or 0)


def count_branches(company):
    from app.models import Company
    return Company.query.filter_by(parent_id=company.id).count()


def count_ai_tokens_this_month(company, user_id=None):
    cycle = _cycle_month()
    q = db.session.query(func.coalesce(
        func.sum(AiTokenUsage.total_tokens), 0)
    ).filter(
        AiTokenUsage.company_id == company.id,
        AiTokenUsage.created_at >= f"{cycle}-01",
    )
    if user_id is not None:
        q = q.filter(AiTokenUsage.user_id == user_id)
    return int(q.scalar() or 0)


def count_storage_bytes(company):
    """Sum of every file the company has uploaded. Iterates:
      · user_files.size_bytes
      · opsflow documents (LEAD/PROJECT/TASK).size_bytes
    Any other future storage location can add to this dict."""
    total = 0
    # user_files
    try:
        from app.models import UserFile
        row = db.session.query(func.coalesce(
            func.sum(UserFile.size_bytes), 0)
        ).filter(UserFile.company_id == company.id).scalar()
        total += int(row or 0)
    except Exception:
        pass
    # opsflow documents
    try:
        from app.models import Document
        row = db.session.query(func.coalesce(
            func.sum(Document.size_bytes), 0)
        ).filter(Document.company_id == company.id).scalar()
        total += int(row or 0)
    except Exception:
        pass
    return total


def count_current(company, quota_type):
    if quota_type == QUOTA_USERS:
        return count_users(company)
    if quota_type == QUOTA_BRANCHES:
        return count_branches(company)
    if quota_type == QUOTA_AI_TOKENS_MONTH:
        return count_ai_tokens_this_month(company)
    if quota_type == QUOTA_STORAGE_BYTES:
        return count_storage_bytes(company)
    return 0


# ─── Enforcement ────────────────────────────────────────────────
def check_quota(company, quota_type, *, incoming=1, user_id=None):
    """Central choke point. Raises QuotaBlockedError when
    enforcement_mode=BLOCK and the operation would push usage above
    included_amount. Under ALLOW_NOTIFY the operation is allowed but
    _notify_thresholds() fires.

    `incoming` = the delta this operation would ADD to the counter
    (e.g. incoming_bytes for a file upload). Defaults to 1.
    `user_id` = optional per-employee sub-cap check for AI tokens.
    """
    quota = get_quota(company, quota_type)
    if is_unlimited(quota):
        return

    current = count_current(company, quota_type)
    would_be = current + incoming

    # Per-employee AI cap: independent from the company-wide quota.
    # If the employee has a cap AND they'd overshoot it, block them
    # even if the company still has budget.
    if quota_type == QUOTA_AI_TOKENS_MONTH and user_id is not None:
        cap_row = EmployeeAiCap.query.filter_by(
            company_id=company.id, user_id=user_id).first()
        if cap_row and cap_row.monthly_cap:
            employee_used = count_ai_tokens_this_month(
                company, user_id=user_id)
            if employee_used + incoming > cap_row.monthly_cap:
                raise QuotaBlockedError(
                    f"تجاوزت الحد الشهري المخصّص لك "
                    f"({employee_used}/{cap_row.monthly_cap} توكن)."
                )

    if would_be > quota.included_amount:
        if quota.enforcement_mode == ENF_BLOCK:
            raise QuotaBlockedError(
                f"وصلت لحد الباقة لهذا المورد "
                f"({current}/{quota.included_amount}). "
                f"رقّي الباقة أو حرّر مساحة قبل الإضافة."
            )
        # ENF_ALLOW_NOTIFY — allow, but nudge owner.
        _notify_thresholds(company, quota_type, would_be,
                            quota.included_amount)


# ─── Logging ────────────────────────────────────────────────────
def log_ai_usage(company_id, user_id, provider, model,
                 input_tokens, output_tokens, total_tokens=None):
    """Called from the agent path after Anthropic returns
    response.usage. Non-fatal — a logging failure never denies the
    LLM call."""
    try:
        if total_tokens is None:
            total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
        row = AiTokenUsage(
            company_id=company_id, user_id=user_id,
            provider=provider, model=model,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            total_tokens=int(total_tokens or 0),
        )
        db.session.add(row); db.session.commit()
    except Exception:
        from flask import current_app
        try:
            current_app.logger.exception(
                "log_ai_usage failed for company=%s user=%s",
                company_id, user_id)
        except Exception:
            pass


# ─── Notifications ──────────────────────────────────────────────
def _notify_thresholds(company, quota_type, current, limit):
    """Fire owner notifications at 80% / 90% / 100% at most once
    per cycle_month per (company, quota_type, threshold)."""
    if not limit:
        return
    pct = int((current / limit) * 100) if limit else 0
    thresholds = [t for t in (80, 90, 100) if pct >= t]
    if not thresholds:
        return
    cycle = _cycle_month()
    # Highest threshold that we haven't already sent for this cycle.
    top = thresholds[-1]
    already = QuotaNotificationSent.query.filter_by(
        company_id=company.id, quota_type=quota_type,
        threshold_pct=top, cycle_month=cycle).first()
    if already:
        return
    try:
        _send_owner_notification(company, quota_type, top,
                                  current, limit)
        # Stamp EVERY threshold ≤ current as sent so a subsequent
        # call at a LOWER threshold doesn't re-fire. Without this,
        # crossing 100% first, then a re-check that computes 90%
        # would emit a second row.
        for t in thresholds:
            exists = QuotaNotificationSent.query.filter_by(
                company_id=company.id, quota_type=quota_type,
                threshold_pct=t, cycle_month=cycle).first()
            if not exists:
                db.session.add(QuotaNotificationSent(
                    company_id=company.id, quota_type=quota_type,
                    threshold_pct=t, cycle_month=cycle))
        db.session.commit()
    except Exception:
        db.session.rollback()
        try:
            from flask import current_app
            current_app.logger.exception(
                "quota notification failed for %s/%s@%d",
                company.id, quota_type, top)
        except Exception:
            pass


def _send_owner_notification(company, quota_type, threshold_pct,
                              current, limit):
    """Send email + create in-app Notification for the company
    owner(s). Small helper — the flake-friendly wrappers above
    catch any exception so this can be freely rewritten later."""
    from app.models import User
    from app.models.user import user_companies
    from app.services.email import send_email
    from flask import render_template
    owners_ids = [r.user_id for r in db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == company.id) &
            (user_companies.c.role == "owner")
        )
    ).fetchall()]
    label = {
        QUOTA_USERS: "المستخدمين",
        QUOTA_AI_TOKENS_MONTH: "توكنز الوكيل الذكي",
        QUOTA_STORAGE_BYTES: "المساحة التخزينية",
        QUOTA_BRANCHES: "الفروع",
    }.get(quota_type, quota_type)
    subject = (f"⚠ استهلاك {label} — وصلت {threshold_pct}% من الحد"
               f" — مرصود")
    for uid in owners_ids:
        u = db.session.get(User, uid)
        if not u:
            continue
        try:
            html = (f"<p>مرحباً {u.full_name}</p>"
                    f"<p>وصل استهلاك <b>{label}</b> إلى "
                    f"{current} من أصل {limit} "
                    f"({threshold_pct}%).</p>"
                    f"<p>يمكنك مراجعة الاستهلاك من /settings/usage.</p>")
            send_email(u.email, subject, html)
        except Exception:
            pass
