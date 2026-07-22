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
        try:
            name = hard_delete_company(company, actor_id=current_user.id,
                                        reason=reason)
        except Exception as e:
            # Surface the cascade failure to the super-admin instead of a 500.
            # The PAL row recording the attempt + the blocking table was
            # already written by the service before the exception bubbled.
            db.session.rollback()
            flash(
                f"تعذّر الحذف النهائي للشركة '{company.name}': "
                f"{type(e).__name__}: {str(e)[:200]} — "
                f"الشركة لسه soft-deleted (قابلة للاستعادة).",
                "error",
            )
            return redirect(url_for("superadmin.company_detail",
                                     company_id=company.id))
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
    # MARSOUD-PASSWORD-POLICY — same policy everywhere.
    from app.services.password_policy import validate_password
    ok, reason = validate_password(new_pw)
    if not ok:
        flash(reason, "error")
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
        # MARSOUD-USER-FILES-CASCADE — same rationale as in
        # users.revoke: wipe the user's uploads for THIS company
        # before we sever the membership, so no orphan bytes stay
        # under private_uploads/user_files/<co>/<user_id>/.
        from app.services.user_files import delete_all_for_user_in_company
        try:
            delete_all_for_user_in_company(
                company_id=company_id, user_id=user_id,
            )
        except Exception:
            import logging
            logging.getLogger("marsoud.user_files").exception(
                "cascade sweep failed on unlink user=%s co=%s",
                user_id, company_id,
            )
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
               "crm", "hr", "reports", "agent",
               "manufacturing", "employee_reports"]
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
    "manufacturing": "التصنيع",
    "employee_reports": "تقارير الموظفين اليومية",
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


def _upsert_plan_price(plan, currency, monthly_raw, yearly_raw):
    """MARSOUD-MULTI-CURRENCY-PRICING — helper used by plans_new /
    plans_edit. Upserts a plan_prices row per currency. Empty inputs
    delete the row so the plan-price resolver falls back to legacy
    columns / other currencies cleanly."""
    from app.models import PlanPrice
    monthly = (monthly_raw or "").strip()
    yearly = (yearly_raw or "").strip()
    row = PlanPrice.query.filter_by(
        plan_id=plan.id, currency=currency).first()
    m_val = float(monthly) if monthly else None
    y_val = float(yearly) if yearly else None
    if m_val is None and y_val is None:
        if row:
            db.session.delete(row)
        return
    if not row:
        row = PlanPrice(plan_id=plan.id, currency=currency)
        db.session.add(row)
    row.price_monthly = m_val
    row.price_yearly = y_val


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
        db.session.flush()
        # MARSOUD-MULTI-CURRENCY-PRICING — accept SAR alongside EGP.
        _upsert_plan_price(
            p, "SAR",
            request.form.get("price_monthly_sar"),
            request.form.get("price_yearly_sar"))
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
        # MARSOUD-MULTI-CURRENCY-PRICING — SAR is optional; empty
        # inputs clean up the row so price_for falls back to EGP.
        _upsert_plan_price(
            p, "SAR",
            request.form.get("price_monthly_sar"),
            request.form.get("price_yearly_sar"))
        db.session.commit()
        log_platform_action("plan_edit", details=f"code={p.code}",
                            actor_id=current_user.id)
        flash(f"تم حفظ الباقة: {p.name_ar}", "success")
        return redirect(url_for("superadmin.plans_index"))
    from app.services.plan_gating import (
        SUB_ITEM_CATALOG, SECTION_LABEL_AR, SECTION_REQUIRES_MODULES,
    )
    # MARSOUD-MULTI-CURRENCY-PRICING — SAR row (may be None on first
    # visit) so the template can pre-fill the SAR inputs.
    from app.models import PlanPrice
    sar_price = PlanPrice.query.filter_by(
        plan_id=p.id, currency="SAR").first()
    return render_template("admin/plans_form.html", plan=p,
                           sar_price=sar_price,
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

# MARSOUD-CUSTOMER-BROADCAST-CENTER (Abdelhamid 2026-07-22) — compose,
# preview count, send.
@bp.route("/broadcasts")
@login_required
@superadmin_required
def broadcasts_index():
    from app.models import Broadcast
    rows = Broadcast.query.order_by(Broadcast.created_at.desc()).all()
    return render_template("admin/broadcasts_index.html", rows=rows)


@bp.route("/broadcasts/new", methods=["GET", "POST"])
@login_required
@superadmin_required
def broadcasts_new():
    from app.models import (
        Broadcast, Plan,
        AUDIENCE_ALL, AUDIENCE_TRIAL, AUDIENCE_ACTIVE,
        AUDIENCE_EXPIRED, AUDIENCE_BY_PLAN,
    )
    from app.services.broadcasts import preview_count
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        body_html = (request.form.get("body_html") or "").strip()
        if not title or not body_html:
            flash("العنوان والمحتوى مطلوبان.", "error")
            return redirect(url_for("superadmin.broadcasts_new"))
        kind = request.form.get("audience_kind", AUDIENCE_ALL)
        filter_dict = {"kind": kind}
        if kind == AUDIENCE_BY_PLAN:
            filter_dict["plan_id"] = int(
                request.form.get("plan_id") or 0)
        channels = request.form.getlist("channels") or ["INAPP"]
        b = Broadcast(title=title, body_html=body_html,
                      sent_by_id=current_user.id)
        b.set_audience(filter_dict)
        b.set_channels(channels)
        db.session.add(b); db.session.commit()
        flash(f"تم إنشاء الرسالة. الجمهور: {preview_count(filter_dict)} مستخدم",
              "success")
        return redirect(url_for("superadmin.broadcasts_index"))
    plans = Plan.query.filter_by(is_active=True).all()
    return render_template("admin/broadcasts_form.html", plans=plans)


@bp.route("/broadcasts/<int:broadcast_id>/preview")
@login_required
@superadmin_required
def broadcasts_preview(broadcast_id):
    from app.models import Broadcast
    from app.services.broadcasts import preview_count
    from flask import jsonify
    b = db.session.get(Broadcast, broadcast_id) or _404()
    return jsonify({"count": preview_count(b.audience)})


@bp.route("/broadcasts/<int:broadcast_id>/send", methods=["POST"])
@login_required
@superadmin_required
def broadcasts_send(broadcast_id):
    from app.models import Broadcast
    from app.services.broadcasts import send, BroadcastError
    b = db.session.get(Broadcast, broadcast_id) or _404()
    try:
        sent, failed = send(b)
        flash(f"تم الإرسال لـ {sent} مستخدم"
              + (f" (فشل {failed})" if failed else ""),
              "success")
    except BroadcastError as e:
        flash(str(e), "error")
    return redirect(url_for("superadmin.broadcasts_index"))


# MARSOUD-DISCOUNT-COUPONS (Abdelhamid 2026-07-22) — CRUD + stats.
@bp.route("/coupons")
@login_required
@superadmin_required
def coupons_index():
    from app.models import Coupon, CouponRedemption
    coupons = Coupon.query.order_by(Coupon.created_at.desc()).all()
    stats = {}
    for c in coupons:
        redemptions = CouponRedemption.query.filter_by(
            coupon_id=c.id).all()
        stats[c.id] = {
            "uses": len(redemptions),
            "saved": sum((float(r.amount_saved) for r in redemptions), 0.0),
            "last_used": max((r.redeemed_at for r in redemptions),
                              default=None),
        }
    return render_template("admin/coupons_index.html",
                           coupons=coupons, stats=stats)


@bp.route("/coupons/new", methods=["GET", "POST"])
@login_required
@superadmin_required
def coupons_new():
    from app.models import Coupon, Plan, DISCOUNT_PERCENT, DISCOUNT_FIXED
    from datetime import datetime as _dt
    if request.method == "POST":
        code = (request.form.get("code") or "").strip().upper()
        if not code:
            flash("الكود مطلوب.", "error")
            return redirect(url_for("superadmin.coupons_new"))
        if Coupon.query.filter_by(code=code).first():
            flash("الكود موجود بالفعل.", "error")
            return redirect(url_for("superadmin.coupons_new"))
        dtype = request.form.get("discount_type", DISCOUNT_PERCENT)
        if dtype not in (DISCOUNT_PERCENT, DISCOUNT_FIXED):
            dtype = DISCOUNT_PERCENT
        try:
            dvalue = float(request.form.get("discount_value") or 0)
        except ValueError:
            dvalue = 0
        max_uses_raw = (request.form.get("max_uses") or "").strip()
        max_per_raw = (request.form.get("max_uses_per_customer") or "1").strip()
        valid_from_raw = (request.form.get("valid_from") or "").strip()
        valid_until_raw = (request.form.get("valid_until") or "").strip()
        def _parse_date(s):
            try:
                return _dt.strptime(s, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                return None
        c = Coupon(
            code=code,
            discount_type=dtype,
            discount_value=dvalue,
            valid_from=_parse_date(valid_from_raw),
            valid_until=_parse_date(valid_until_raw),
            max_uses=int(max_uses_raw) if max_uses_raw.isdigit() else None,
            max_uses_per_customer=int(max_per_raw) if max_per_raw.isdigit() else 1,
            active=True,
            created_by_id=current_user.id,
        )
        plan_ids = request.form.getlist("plan_ids")
        if plan_ids:
            c.set_plan_ids(plan_ids)
        db.session.add(c); db.session.commit()
        log_platform_action("coupon_create", details=f"code={code}",
                            actor_id=current_user.id)
        flash(f"تم إنشاء كود: {code}", "success")
        return redirect(url_for("superadmin.coupons_index"))
    plans = Plan.query.filter_by(is_active=True).all()
    return render_template("admin/coupons_form.html", plans=plans)


@bp.route("/coupons/<int:coupon_id>/toggle", methods=["POST"])
@login_required
@superadmin_required
def coupons_toggle(coupon_id):
    from app.models import Coupon
    c = db.session.get(Coupon, coupon_id) or _404()
    c.active = not c.active
    db.session.commit()
    return redirect(url_for("superadmin.coupons_index"))


# MARSOUD-INACTIVE-COMPANIES-MONITORING (Abdelhamid 2026-07-22) —
# list stale tenants filtered by inactivity window.
@bp.route("/companies/inactive")
@login_required
@superadmin_required
def companies_inactive():
    from datetime import timedelta
    since_arg = (request.args.get("since") or "7d").strip().lower()
    now = datetime.utcnow()
    if since_arg == "never":
        q = Company.query.filter(Company.last_activity_at.is_(None))
    else:
        raw = since_arg.rstrip("d")
        try:
            days = int(raw)
        except ValueError:
            days = 7
        cutoff = now - timedelta(days=days)
        q = Company.query.filter(
            Company.last_activity_at < cutoff
        )
    q = q.filter(Company.deleted_at.is_(None))
    companies = q.order_by(Company.last_activity_at.asc().nullsfirst()).all()
    return render_template(
        "admin/companies_inactive.html",
        companies=companies, since=since_arg, now=now,
    )


# MARSOUD-FEATURE-FLAGS-KILL-SWITCH (Abdelhamid 2026-07-22) — runtime
# module on/off. Super-admin picks a module + optional reason;
# every non-super-admin request into that module gets the friendly
# 503 page. Cache invalidates immediately on save.
@bp.route("/feature-flags", methods=["GET", "POST"])
@login_required
@superadmin_required
def feature_flags_index():
    from app.services.feature_flags import (
        set_module, is_module_enabled, disabled_reason,
    )
    from app.services.plan_gating import _PREFIX_TO_MODULE
    if request.method == "POST":
        module_key = (request.form.get("module_key") or "").strip()
        if not module_key:
            flash("Module key مطلوب.", "error")
            return redirect(url_for("superadmin.feature_flags_index"))
        enabled = request.form.get("enabled") == "on"
        reason = (request.form.get("reason") or "").strip() or None
        set_module(module_key, enabled, reason, current_user.id)
        flash(
            "✅ فُعّل" if enabled else "🚫 توقّف",
            "success",
        )
        return redirect(url_for("superadmin.feature_flags_index"))

    modules = sorted(set(_PREFIX_TO_MODULE.values()))
    rows = [
        {"module_key": m,
         "enabled": is_module_enabled(m),
         "reason": disabled_reason(m)}
        for m in modules
    ]
    return render_template("admin/feature_flags.html", rows=rows)


# MARSOUD-CONSENT-AUDIT-LOG (Abdelhamid 2026-07-22) — cross-tenant
# consent history + "not accepted current version" report.
@bp.route("/consent")
@login_required
@superadmin_required
def consent_index():
    from app.models import ConsentEvent, User
    from app.services.legal import (
        get_terms_version, users_missing_current_version,
    )
    q = ConsentEvent.query
    version_filter = (request.args.get("version") or "").strip()
    email_filter = (request.args.get("email") or "").strip().lower()
    if version_filter:
        q = q.filter(ConsentEvent.document_version == version_filter)
    if email_filter:
        q = q.join(User).filter(User.email.ilike(f"%{email_filter}%"))
    events = q.order_by(ConsentEvent.created_at.desc()).limit(200).all()
    missing = users_missing_current_version() if request.args.get(
        "show_missing") == "1" else []
    return render_template(
        "admin/consent_index.html",
        events=events,
        current_version=get_terms_version(),
        version_filter=version_filter,
        email_filter=email_filter,
        missing=missing,
    )


@bp.route("/users/<int:user_id>/consent")
@login_required
@superadmin_required
def user_consent(user_id):
    from app.models import User, ConsentEvent
    u = db.session.get(User, user_id) or _404()
    events = ConsentEvent.query.filter_by(user_id=user_id).order_by(
        ConsentEvent.created_at.desc()).all()
    return render_template("admin/user_consent.html",
                           u=u, events=events)


# MARSOUD-TERMS-CONSENT (Abdelhamid 2026-07-22) — content editor
# for /terms + /privacy. Super-admin only. Publishing a NEW version
# forces every user to re-accept on their next request.
@bp.route("/legal", methods=["GET", "POST"])
@login_required
@superadmin_required
def legal():
    from app.services.legal import (
        get_terms_version, get_terms_html, get_privacy_html,
        set_legal, DEFAULT_TERMS_VERSION,
    )
    if request.method == "POST":
        version = (request.form.get("terms_version") or "").strip() or DEFAULT_TERMS_VERSION
        terms_html = request.form.get("terms_html") or ""
        privacy_html = request.form.get("privacy_html") or ""
        set_legal(version, terms_html, privacy_html)
        db.session.commit()
        log_platform_action(
            "legal_publish", actor_id=current_user.id,
            details=f"version={version}, "
                    f"terms_len={len(terms_html)}, "
                    f"privacy_len={len(privacy_html)}")
        flash("تم نشر الإصدار — سيُطلب من المستخدمين الموافقة عليه.",
              "success")
        return redirect(url_for("superadmin.legal"))
    return render_template(
        "admin/legal.html",
        terms_version=get_terms_version(),
        terms_html=get_terms_html(),
        privacy_html=get_privacy_html(),
    )


@bp.route("/subscription-settings", methods=["GET", "POST"])
@login_required
@superadmin_required
def subscription_settings():
    from app.services.subscription import (
        get_reminder_thresholds, set_reminder_thresholds,
        get_grace_days, set_grace_days,
        get_readonly_enabled, set_readonly_enabled,
        get_trial_days, set_trial_days,
        DEFAULT_REMINDER_THRESHOLDS, DEFAULT_GRACE_DAYS,
        DEFAULT_READONLY_ENABLED, DEFAULT_SUBSCRIPTION_DAYS,
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

        # MARSOUD-TRIAL-DAYS-SETTING — accept the trial length. Silently
        # ignored if the field is missing or out of range so older POSTs
        # (missing the field) don't blow away a previously-saved value.
        trial_raw = request.form.get("trial_days", "").strip()
        if trial_raw.isdigit() and 1 <= int(trial_raw) <= 365:
            set_trial_days(int(trial_raw))

        # MARSOUD-API-RATE-LIMIT — per-token requests-per-minute cap.
        # Same pattern as trial_days: silently skip when missing or
        # out of range so older POSTs don't blow away the value.
        rate_raw = request.form.get("api_rate_limit_per_minute", "").strip()
        if rate_raw.isdigit() and 1 <= int(rate_raw) <= 100_000:
            from app.services.subscription import _set_setting_raw
            _set_setting_raw("api_rate_limit_per_minute", str(int(rate_raw)))

        db.session.commit()
        log_platform_action("subscription_settings_update",
                            actor_id=current_user.id,
                            details=(f"thresholds={nums}, grace={grace}, "
                                     f"trial_days={trial_raw or '—'}"))
        flash("تم حفظ إعدادات الاشتراك", "success")
        return redirect(url_for("superadmin.subscription_settings"))

    # MARSOUD-API-RATE-LIMIT — surface the current per-minute cap.
    from app.services.rate_limit import (
        _limit_per_minute, DEFAULT_LIMIT_PER_MINUTE,
    )
    return render_template(
        "admin/subscription_settings.html",
        thresholds=get_reminder_thresholds(),
        grace_days=get_grace_days(),
        readonly_enabled=get_readonly_enabled(),
        trial_days=get_trial_days(),
        default_thresholds=DEFAULT_REMINDER_THRESHOLDS,
        default_grace=DEFAULT_GRACE_DAYS,
        default_readonly=DEFAULT_READONLY_ENABLED,
        default_trial_days=DEFAULT_SUBSCRIPTION_DAYS,
        api_rate_limit_per_minute=_limit_per_minute(),
        default_api_rate_limit=DEFAULT_LIMIT_PER_MINUTE,
    )
