"""Super-admin blueprint — mounted at /admin.

All routes are guarded by @superadmin_required (403 for everyone else) and
operate cross-company (no tenant filter). Every state-changing action writes
a PlatformAuditLog entry.
"""
from datetime import datetime
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, session, g,
)
from flask_login import current_user, login_required
from sqlalchemy import or_
from app import db
from app.models import (
    User, Company, PlatformAuditLog, SuperadminImpersonation, Invitation,
    user_companies,
)
from app.services.superadmin import (
    superadmin_required, log_platform_action, platform_overview,
    companies_with_stats, users_with_companies,
    start_impersonation, end_impersonation,
)

bp = Blueprint("superadmin", __name__, template_folder="../templates")


# ── Ticket 2: dashboard ──────────────────────────────────────────────────── #
@bp.route("/")
@login_required
@superadmin_required
def dashboard():
    data = platform_overview()
    return render_template("admin/dashboard.html", **data)


# ── Ticket 3: companies management ───────────────────────────────────────── #
@bp.route("/companies")
@login_required
@superadmin_required
def companies():
    q = (request.args.get("q") or "").strip()
    rows = companies_with_stats()
    if q:
        rows = [r for r in rows if q.lower() in (r["company"].name or "").lower()]
    return render_template("admin/companies.html", rows=rows, q=q)


@bp.route("/companies/<int:company_id>")
@login_required
@superadmin_required
def company_detail(company_id):
    company = db.session.get(Company, company_id) or _404()
    company_users = (db.session.query(User, user_companies.c.role)
                     .join(user_companies, user_companies.c.user_id == User.id)
                     .filter(user_companies.c.company_id == company_id)
                     .all())
    recent_activity = (PlatformAuditLog.query
                       .filter(PlatformAuditLog.target_company_id == company_id)
                       .order_by(PlatformAuditLog.created_at.desc())
                       .limit(25).all())
    return render_template("admin/company_detail.html",
                           company=company,
                           company_users=company_users,
                           recent_activity=recent_activity)


@bp.route("/companies/<int:company_id>/toggle", methods=["POST"])
@login_required
@superadmin_required
def company_toggle(company_id):
    company = db.session.get(Company, company_id) or _404()
    suspending = (company.status or "ACTIVE") != "SUSPENDED"
    company.status = "SUSPENDED" if suspending else "ACTIVE"
    company.is_active = not suspending  # keep legacy flag in sync
    db.session.commit()
    log_platform_action(
        "company_suspend" if suspending else "company_activate",
        target_company_id=company_id,
    )
    flash(f"تم {'إيقاف' if suspending else 'تفعيل'} الشركة", "success")
    return redirect(url_for("superadmin.companies"))


@bp.route("/companies/<int:company_id>/edit", methods=["GET", "POST"])
@login_required
@superadmin_required
def company_edit(company_id):
    company = db.session.get(Company, company_id) or _404()
    if request.method == "POST":
        company.name = request.form.get("name", company.name).strip()
        company.base_currency = request.form.get("base_currency",
                                                 company.base_currency).strip()
        try:
            company.vat_rate = float(request.form.get("vat_rate") or company.vat_rate or 0)
        except ValueError:
            pass
        company.tax_number = request.form.get("tax_number") or company.tax_number
        new_status = (request.form.get("status") or company.status or "ACTIVE").upper()
        if new_status in ("ACTIVE", "SUSPENDED", "TRIAL"):
            company.status = new_status
            company.is_active = (new_status != "SUSPENDED")
        new_plan = (request.form.get("plan") or company.plan or "FREE").upper()
        if new_plan in ("FREE", "PRO", "ENTERPRISE"):
            company.plan = new_plan
        db.session.commit()
        log_platform_action("company_edit", target_company_id=company_id,
                            details=f"name={company.name}")
        flash("تم حفظ إعدادات الشركة", "success")
        return redirect(url_for("superadmin.company_detail",
                                company_id=company_id))
    return render_template("admin/company_edit.html", company=company)


@bp.route("/companies/<int:company_id>/delete", methods=["POST"])
@login_required
@superadmin_required
def company_delete(company_id):
    company = db.session.get(Company, company_id) or _404()
    name = company.name
    log_platform_action("company_delete", target_company_id=company_id,
                        details=name)
    db.session.delete(company)
    db.session.commit()
    flash(f"تم حذف الشركة: {name}", "success")
    return redirect(url_for("superadmin.companies"))


# ── Ticket 4: users management ───────────────────────────────────────────── #
@bp.route("/users")
@login_required
@superadmin_required
def users():
    q = (request.args.get("q") or "").strip().lower()
    rows = users_with_companies()
    if q:
        rows = [u for u in rows
                if q in (u.email or "").lower() or q in (u.full_name or "").lower()]
    return render_template("admin/users.html", rows=rows, q=q)


@bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@superadmin_required
def user_toggle(user_id):
    user = db.session.get(User, user_id) or _404()
    user.is_active = not bool(user.is_active)
    db.session.commit()
    log_platform_action(
        "user_suspend" if not user.is_active else "user_activate",
        target_user_id=user_id,
    )
    flash(
        f"تم {'إيقاف' if not user.is_active else 'تفعيل'} المستخدم",
        "success",
    )
    return redirect(url_for("superadmin.users"))


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@superadmin_required
def user_reset_password(user_id):
    user = db.session.get(User, user_id) or _404()
    new_pw = (request.form.get("new_password") or "").strip()
    if len(new_pw) < 6:
        flash("كلمة المرور يجب أن تكون 6 أحرف على الأقل", "error")
        return redirect(url_for("superadmin.users"))
    user.set_password(new_pw)
    db.session.commit()
    log_platform_action("user_reset_password", target_user_id=user_id)
    flash(f"تم إعادة تعيين كلمة المرور لـ {user.email}", "success")
    return redirect(url_for("superadmin.users"))


@bp.route("/users/<int:user_id>/unlink/<int:company_id>", methods=["POST"])
@login_required
@superadmin_required
def user_unlink(user_id, company_id):
    user = db.session.get(User, user_id) or _404()
    company = db.session.get(Company, company_id) or _404()
    if company in user.companies:
        user.companies.remove(company)
        db.session.commit()
        log_platform_action("user_unlink_from_company",
                            target_user_id=user_id,
                            target_company_id=company_id)
        flash("تم فك الربط", "success")
    return redirect(request.referrer or url_for("superadmin.users"))


@bp.route("/users/<int:user_id>/resend-invite", methods=["POST"])
@login_required
@superadmin_required
def user_resend_invite(user_id):
    from app.services.email import send_invitation_email
    user = db.session.get(User, user_id) or _404()
    pending = (Invitation.query
               .filter(Invitation.email == user.email,
                       Invitation.accepted_at.is_(None),
                       Invitation.revoked_at.is_(None))
               .order_by(Invitation.created_at.desc()).first())
    if not pending:
        log_platform_action("user_resend_invite", target_user_id=user_id,
                            details="no_pending_invite")
        flash("لا توجد دعوة معلقة لهذا المستخدم", "info")
        return redirect(url_for("superadmin.users"))
    accept_url = url_for("invitations.accept", token=pending.token,
                         _external=True)
    sent = send_invitation_email(pending, accept_url)
    log_platform_action("user_resend_invite", target_user_id=user_id,
                        target_company_id=pending.company_id,
                        details=f"sent={bool(sent)}")
    if sent:
        flash(f"تم إعادة إرسال الدعوة لـ {user.email}", "success")
    else:
        flash(f"تم تجهيز الدعوة (وضع التطوير): {accept_url}", "info")
    return redirect(url_for("superadmin.users"))


# ── Ticket 5: activity / audit log ───────────────────────────────────────── #
@bp.route("/audit")
@login_required
@superadmin_required
def audit():
    from datetime import datetime, timedelta
    from app.models.journal_extras import JournalAudit
    from app.models import JournalEntry
    company_id = request.args.get("company_id", type=int)
    user_id = request.args.get("user_id", type=int)
    action = (request.args.get("action") or "").strip()
    date_from = request.args.get("date_from") or ""
    date_to = request.args.get("date_to") or ""

    def _parse(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
    d_from = _parse(date_from)
    d_to = _parse(date_to)
    if d_to:
        d_to = d_to + timedelta(days=1)  # inclusive

    q = PlatformAuditLog.query
    if company_id:
        q = q.filter(PlatformAuditLog.target_company_id == company_id)
    if user_id:
        q = q.filter(or_(PlatformAuditLog.actor_id == user_id,
                         PlatformAuditLog.target_user_id == user_id))
    if action:
        q = q.filter(PlatformAuditLog.action == action)
    if d_from:
        q = q.filter(PlatformAuditLog.created_at >= d_from)
    if d_to:
        q = q.filter(PlatformAuditLog.created_at < d_to)
    platform_rows = q.order_by(PlatformAuditLog.created_at.desc()).limit(500).all()

    # ── Union with tenant-level JournalAudit ─────────────────────────
    ja_q = JournalAudit.query.join(JournalEntry,
                                   JournalEntry.id == JournalAudit.entry_id)
    if company_id:
        ja_q = ja_q.filter(JournalEntry.company_id == company_id)
    if user_id:
        ja_q = ja_q.filter(JournalAudit.user_id == user_id)
    if d_from:
        ja_q = ja_q.filter(JournalAudit.created_at >= d_from)
    if d_to:
        ja_q = ja_q.filter(JournalAudit.created_at < d_to)
    journal_audit_rows = (ja_q.order_by(JournalAudit.created_at.desc())
                          .limit(200).all())

    # Combine into a single unified list ordered by created_at desc.
    unified = []
    for r in platform_rows:
        unified.append({
            "source": "platform", "action": r.action,
            "actor_email": r.actor.email if r.actor else None,
            "target_company": r.target_company.name if r.target_company else None,
            "target_user_email": r.target_user.email if r.target_user else None,
            "ip": r.ip_address, "details": r.details,
            "created_at": r.created_at,
        })
    for r in journal_audit_rows:
        if action and r.action.value != action:
            continue
        unified.append({
            "source": "journal", "action": "journal." + r.action.value,
            "actor_email": r.user.email if r.user else None,
            "target_company": (r.entry.company.name
                               if r.entry and r.entry.company else None),
            "target_user_email": None,
            "ip": None, "details": r.reason,
            "created_at": r.created_at,
        })
    unified.sort(key=lambda x: x["created_at"], reverse=True)
    unified = unified[:500]

    actions = sorted({r["action"] for r in unified})
    companies_list = Company.query.order_by(Company.name).all()
    return render_template("admin/audit.html", rows=unified,
                           actions=actions, companies_list=companies_list,
                           selected_company=company_id,
                           selected_user=user_id,
                           selected_action=action,
                           date_from=date_from, date_to=date_to)


# ── Ticket 6: support tools (view-as) ────────────────────────────────────── #
@bp.route("/companies/<int:company_id>/view-as", methods=["POST"])
@login_required
@superadmin_required
def view_as(company_id):
    company = db.session.get(Company, company_id) or _404()
    start_impersonation(company_id, reason=request.form.get("reason"))
    flash(f"دخلت كشركة: {company.name} — وضع قراءة فقط", "info")
    return redirect(url_for("dashboard.index"))


@bp.route("/view-as/stop", methods=["POST"])
@login_required
def view_as_stop():
    """Anyone can stop their own impersonation (decorator-light on purpose)."""
    end_impersonation()
    flash("تم إنهاء وضع المعاينة", "success")
    return redirect(url_for("superadmin.dashboard"))


@bp.route("/errors")
@login_required
@superadmin_required
def errors_global():
    from app.models import PlatformError
    rows = (PlatformError.query
            .order_by(PlatformError.created_at.desc())
            .limit(200).all())
    return render_template("admin/errors.html", rows=rows, scope_company=None)


@bp.route("/companies/<int:company_id>/errors")
@login_required
@superadmin_required
def errors_for_company(company_id):
    from app.models import PlatformError
    company = db.session.get(Company, company_id) or _404()
    rows = (PlatformError.query
            .filter(PlatformError.company_id == company_id)
            .order_by(PlatformError.created_at.desc())
            .limit(200).all())
    return render_template("admin/errors.html", rows=rows, scope_company=company)


@bp.route("/impersonations")
@login_required
@superadmin_required
def impersonations():
    rows = (SuperadminImpersonation.query
            .order_by(SuperadminImpersonation.started_at.desc())
            .limit(200).all())
    return render_template("admin/impersonations.html", rows=rows)


def _404():
    from flask import abort
    abort(404)
