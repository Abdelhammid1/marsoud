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
    user_companies, Plan, SubscriptionReminderSent,
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
    sort = (request.args.get("sort") or "").strip()
    from datetime import datetime
    _min = datetime.min
    if sort == "activity":
        rows.sort(key=lambda r: r.get("last_activity") or _min, reverse=True)
    elif sort == "created_asc":
        rows.sort(key=lambda r: r["company"].created_at or _min)
    elif sort == "created_desc":
        rows.sort(key=lambda r: r["company"].created_at or _min, reverse=True)
    return render_template("admin/companies.html", rows=rows, q=q, sort=sort)


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
    plans = Plan.query.filter_by(is_active=True).order_by(Plan.id).all()
    return render_template("admin/company_detail.html",
                           company=company,
                           company_users=company_users,
                           recent_activity=recent_activity,
                           plans=plans)


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
    """MARSOUD-K — default is SOFT delete (reversible). Pass
    confirm_permanent=1 with a reason to wipe instead."""
    from app.services.lifecycle import (
        soft_delete_company, hard_delete_company,
    )
    company = db.session.get(Company, company_id) or _404()
    reason = (request.form.get("reason") or "").strip() or "(super-admin action)"
    if request.form.get("confirm_permanent") == "1":
        name = hard_delete_company(company, actor_id=current_user.id,
                                    reason=reason)
        flash(f"تم الحذف النهائي للشركة: {name}", "success")
        return redirect(url_for("superadmin.companies"))
    soft_delete_company(company, actor_id=current_user.id, reason=reason)
    flash(f"تم حذف الشركة '{company.name}' (قابلة للاستعادة).", "success")
    return redirect(url_for("superadmin.companies"))


@bp.route("/companies/<int:company_id>/restore", methods=["POST"])
@login_required
@superadmin_required
def company_restore(company_id):
    """MARSOUD-K — reverse a soft delete."""
    from app.services.lifecycle import restore_company
    company = db.session.get(Company, company_id) or _404()
    if restore_company(company, actor_id=current_user.id):
        flash(f"تم استعادة الشركة: {company.name}", "success")
    else:
        flash("الشركة ليست محذوفة", "info")
    return redirect(url_for("superadmin.company_detail",
                             company_id=company.id))


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
    sort = (request.args.get("sort") or "").strip()
    from datetime import datetime
    _min = datetime.min
    rows = list(rows)
    if sort == "login":
        rows.sort(key=lambda u: u.last_login_at or _min, reverse=True)
    elif sort == "created_asc":
        rows.sort(key=lambda u: u.created_at or _min)
    elif sort == "created_desc":
        rows.sort(key=lambda u: u.created_at or _min, reverse=True)
    return render_template("admin/users.html", rows=rows, q=q, sort=sort)


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


# ── MARSOUD-48: email diagnostic ─────────────────────────────────────────── #
@bp.route("/email-test", methods=["GET", "POST"])
@login_required
@superadmin_required
def email_test():
    """Synchronous SMTP probe — sends a test email to whatever address the
    super-admin types in and reports the EXACT result (success / failure +
    full exception text). Bypasses the cron + reminders pipeline so we can
    isolate whether the problem is in send_email itself or in the cron."""
    from flask import current_app
    cfg = current_app.config
    snapshot = {
        "SMTP_HOST": cfg.get("SMTP_HOST") or "(empty — log-only mode)",
        "SMTP_PORT": cfg.get("SMTP_PORT") or "(empty)",
        "SMTP_USER": cfg.get("SMTP_USER") or "(no user)",
        "SMTP_USE_TLS": cfg.get("SMTP_USE_TLS", True),
        "SMTP_FROM": cfg.get("SMTP_FROM") or "no-reply@marsoud.app",
        "SMTP_FROM_NAME": cfg.get("SMTP_FROM_NAME") or "Marsoud",
        "smtp_password_set": bool(cfg.get("SMTP_PASSWORD")),
    }
    result = None
    if request.method == "POST":
        target = (request.form.get("to") or "").strip()
        if not target or "@" not in target:
            result = {"ok": False, "error": "أدخل بريد صالح"}
        else:
            # Send synchronously, capture the EXACT exception if any.
            import smtplib
            import traceback
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            from email.utils import formataddr
            try:
                if not cfg.get("SMTP_HOST"):
                    result = {
                        "ok": False,
                        "error": "SMTP_HOST not configured — emails are log-only. Set SMTP_HOST in .env and restart.",
                    }
                else:
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = "Marsoud — اختبار إرسال إيميل"
                    msg["From"] = formataddr((cfg.get("SMTP_FROM_NAME", "Marsoud"),
                                              cfg.get("SMTP_FROM", "no-reply@marsoud.app")))
                    msg["To"] = target
                    msg.attach(MIMEText(
                        f"<p>هذا اختبار من /admin/email-test بواسطة {current_user.full_name}.</p>"
                        f"<p>الوقت: {datetime.utcnow().isoformat()}Z UTC</p>",
                        "html", "utf-8",
                    ))
                    with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=30) as s:
                        if cfg.get("SMTP_USE_TLS", True):
                            s.starttls()
                        if cfg.get("SMTP_USER"):
                            s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
                        s.send_message(msg)
                    result = {"ok": True, "msg": f"تم الإرسال إلى {target}. افحص صندوق الوارد + spam folder."}
                    log_platform_action(
                        "EMAIL_TEST_SENT",
                        actor_id=current_user.id,
                        details=f"to={target}",
                    )
            except Exception as e:
                result = {
                    "ok": False,
                    "error": f"{type(e).__name__}: {str(e)[:500]}",
                    "trace": traceback.format_exc()[:2000],
                }
    return render_template("admin/email_test.html", snapshot=snapshot, result=result)


@bp.route("/cron-tick", methods=["POST"])
@login_required
@superadmin_required
def cron_tick_now():
    """Run the cron pipeline immediately so the super-admin can see what
    would have happened if the external scheduler had fired. Returns the
    same summary JSON as POST /cron/tick."""
    from app.services.reminders import process_invoice_reminders
    from app.services.invoicing import update_overdue_statuses
    summary = {}
    overdue_total = 0
    for c in Company.query.filter_by(is_active=True).all():
        overdue_total += update_overdue_statuses(c.id)
    summary["marked_overdue"] = overdue_total
    summary["reminders"] = process_invoice_reminders()
    log_platform_action(
        "CRON_MANUAL_TICK",
        actor_id=current_user.id,
        details=str(summary)[:500],
    )
    flash(f"تم تشغيل cron يدوياً: {summary}", "success")
    return redirect(url_for("superadmin.email_test"))


# ─── MARSOUD-57.2: Plans CRUD ────────────────────────────────────────────
ALL_MODULES = ["accounting", "sales", "inventory", "purchases", "pos",
               "crm", "hr", "reports", "agent"]
MODULE_LABELS_AR = {
    "accounting": "المحاسبة",
    "sales": "المبيعات",
    "inventory": "المخزون",
    "purchases": "المشتريات",
    "pos": "نقطة البيع",
    "crm": "العملاء المحتملين (CRM)",
    "hr": "الموارد البشرية",
    "reports": "التقارير",
    "agent": "المحاسب الذكي",
}


def _read_subitems_form():
    """MARSOUD-58 — parse selected sub-items from the plan form. Empty
    list = "lock everything"; missing key = legacy mode (NULL = all)."""
    return request.form.getlist("subitems")


@bp.route("/plans")
@login_required
@superadmin_required
def plans_index():
    plans = Plan.query.order_by(Plan.id).all()
    counts = {p.id: Company.query.filter_by(plan_id=p.id).count() for p in plans}
    return render_template("admin/plans_index.html",
                           plans=plans, counts=counts,
                           all_modules=ALL_MODULES,
                           module_labels=MODULE_LABELS_AR)


@bp.route("/plans/new", methods=["GET", "POST"])
@login_required
@superadmin_required
def plans_new():
    if request.method == "POST":
        code = (request.form.get("code") or "").strip().lower()
        if not code or Plan.query.filter_by(code=code).first():
            flash("الكود مطلوب وفريد", "error")
            return redirect(url_for("superadmin.plans_new"))
        p = Plan(
            code=code,
            name_ar=(request.form.get("name_ar") or "").strip(),
            name=(request.form.get("name") or "").strip(),
            description=(request.form.get("description") or "").strip() or None,
            price_monthly=float(request.form.get("price_monthly") or 0) or None,
            price_yearly=float(request.form.get("price_yearly") or 0) or None,
        )
        p.set_modules(request.form.getlist("modules"))
        # MARSOUD-58 — sub-item list. Form sends a submit_subitems=1 flag
        # so we can distinguish "user opted to enable everything" (no
        # checkboxes shown yet for a new plan) from "user unchecked all".
        if request.form.get("submit_subitems") == "1":
            p.set_subitems(_read_subitems_form())
        db.session.add(p)
        db.session.commit()
        log_platform_action("plan_create", details=f"code={code}",
                            actor_id=current_user.id)
        flash(f"تم إنشاء باقة: {p.name_ar}", "success")
        return redirect(url_for("superadmin.plans_index"))
    from app.services.plan_gating import (
        SUB_ITEM_CATALOG, SECTION_LABEL_AR, SECTION_REQUIRES_MODULES,
    )
    return render_template("admin/plans_form.html", plan=None,
                           all_modules=ALL_MODULES,
                           module_labels=MODULE_LABELS_AR,
                           sub_item_catalog=SUB_ITEM_CATALOG,
                           section_label_ar=SECTION_LABEL_AR,
                           section_requires_modules=SECTION_REQUIRES_MODULES)


@bp.route("/plans/<int:plan_id>/edit", methods=["GET", "POST"])
@login_required
@superadmin_required
def plans_edit(plan_id):
    p = db.session.get(Plan, plan_id) or _404()
    if request.method == "POST":
        p.name_ar = (request.form.get("name_ar") or p.name_ar).strip()
        p.name = (request.form.get("name") or p.name).strip()
        p.description = (request.form.get("description") or "").strip() or None
        p.price_monthly = float(request.form.get("price_monthly") or 0) or None
        p.price_yearly = float(request.form.get("price_yearly") or 0) or None
        p.is_active = request.form.get("is_active") == "on"
        p.set_modules(request.form.getlist("modules"))
        if request.form.get("submit_subitems") == "1":
            p.set_subitems(_read_subitems_form())
        db.session.commit()
        log_platform_action("plan_edit", details=f"code={p.code}",
                            actor_id=current_user.id)
        flash(f"تم حفظ الباقة: {p.name_ar}", "success")
        return redirect(url_for("superadmin.plans_index"))
    from app.services.plan_gating import (
        SUB_ITEM_CATALOG, SECTION_LABEL_AR, SECTION_REQUIRES_MODULES,
    )
    return render_template("admin/plans_form.html", plan=p,
                           all_modules=ALL_MODULES,
                           module_labels=MODULE_LABELS_AR,
                           sub_item_catalog=SUB_ITEM_CATALOG,
                           section_label_ar=SECTION_LABEL_AR,
                           section_requires_modules=SECTION_REQUIRES_MODULES)


@bp.route("/companies/<int:company_id>/assign-plan", methods=["POST"])
@login_required
@superadmin_required
def companies_assign_plan(company_id):
    company = db.session.get(Company, company_id) or _404()
    raw = request.form.get("plan_id")
    new_plan_id = int(raw) if raw and raw.isdigit() else None
    if new_plan_id:
        plan = db.session.get(Plan, new_plan_id) or _404()
        company.plan_id = plan.id
    else:
        company.plan_id = None
    db.session.commit()
    log_platform_action("plan_assign", target_company_id=company_id,
                        details=f"plan_id={new_plan_id}",
                        actor_id=current_user.id)
    flash("تم تحديث الباقة", "success")
    return redirect(url_for("superadmin.company_detail", company_id=company_id))


# ─── MARSOUD-57.3: Subscriptions ─────────────────────────────────────────
@bp.route("/subscriptions")
@login_required
@superadmin_required
def subscriptions_index():
    from datetime import datetime as _dt
    rows = []
    for c in Company.query.filter_by(is_active=True).order_by(Company.name).all():
        expires = c.subscription_expires_at
        days = None
        bucket = "unknown"
        if expires:
            delta = (expires - _dt.utcnow()).days
            days = delta
            if delta < 0:
                bucket = "expired"
            elif delta <= 7:
                bucket = "soon"
            else:
                bucket = "active"
        rows.append({
            "company": c,
            "plan": c.subscription_plan,
            "expires": expires,
            "days_remaining": days,
            "bucket": bucket,
        })
    return render_template("admin/subscriptions_index.html", rows=rows)


@bp.route("/subscriptions/<int:company_id>/renew", methods=["POST"])
@login_required
@superadmin_required
def subscriptions_renew(company_id):
    from datetime import datetime as _dt, timedelta as _td
    company = db.session.get(Company, company_id) or _404()
    period = (request.form.get("period") or "month").lower()
    days = 365 if period == "year" else 30
    base = company.subscription_expires_at
    # Renew from now if already expired, otherwise extend the current expiry.
    if not base or base < _dt.utcnow():
        base = _dt.utcnow()
    company.subscription_expires_at = base + _td(days=days)
    if not company.subscription_started_at:
        company.subscription_started_at = _dt.utcnow()
    # Clear reminder history for the new expiry so future reminders can fire.
    SubscriptionReminderSent.query.filter_by(company_id=company.id).delete()
    db.session.commit()
    log_platform_action("subscription_renew", target_company_id=company.id,
                        details=f"period={period}, new_expires={company.subscription_expires_at}",
                        actor_id=current_user.id)
    flash(f"تم تجديد اشتراك {company.name} لمدة {'سنة' if period=='year' else 'شهر'}", "success")
    return redirect(url_for("superadmin.subscriptions_index"))


# ─── TICKET 1: subscription settings (platform-level) ────────────────────
@bp.route("/subscription-settings", methods=["GET", "POST"])
@login_required
@superadmin_required
def subscription_settings():
    from app.services.subscription import (
        get_reminder_thresholds, set_reminder_thresholds,
        get_grace_days, set_grace_days,
        get_readonly_enabled, set_readonly_enabled,
        DEFAULT_REMINDER_THRESHOLDS, DEFAULT_GRACE_DAYS,
        DEFAULT_READONLY_ENABLED,
    )
    if request.method == "POST":
        raw = request.form.get("reminder_thresholds", "")
        nums = []
        for piece in raw.split(","):
            piece = piece.strip()
            if piece.lstrip("-").isdigit() and 0 <= int(piece) <= 365:
                nums.append(int(piece))
        if not nums:
            nums = list(DEFAULT_REMINDER_THRESHOLDS)
        set_reminder_thresholds(nums)

        grace = request.form.get("grace_days", "").strip()
        if grace.lstrip("-").isdigit() and 0 <= int(grace) <= 365:
            set_grace_days(int(grace))

        set_readonly_enabled(request.form.get("readonly_enabled") == "on")

        db.session.commit()
        log_platform_action("subscription_settings_update",
                            actor_id=current_user.id,
                            details=f"thresholds={nums}, grace={grace}")
        flash("تم حفظ إعدادات الاشتراك", "success")
        return redirect(url_for("superadmin.subscription_settings"))

    return render_template(
        "admin/subscription_settings.html",
        thresholds=get_reminder_thresholds(),
        grace_days=get_grace_days(),
        readonly_enabled=get_readonly_enabled(),
        default_thresholds=DEFAULT_REMINDER_THRESHOLDS,
        default_grace=DEFAULT_GRACE_DAYS,
        default_readonly=DEFAULT_READONLY_ENABLED,
    )
