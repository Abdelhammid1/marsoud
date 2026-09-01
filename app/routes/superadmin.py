"""Super-admin blueprint — mounted at /admin.

All routes are guarded by @superadmin_required (403 for everyone else) and
operate cross-company (no tenant filter). Every state-changing action writes
a PlatformAuditLog entry.
"""
from datetime import datetime
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, session, g,
    current_app, abort,
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
    # MARSOUD-PLATFORM-REVENUE-DASHBOARD (Abdelhamid 2026-07-22).
    from app.services.platform_metrics import (
        mrr, arr, plan_distribution, subscription_states,
        renewals_due, monthly_revenue_series,
    )
    from app.models import Plan
    plan_lookup = {p.id: (p.name_ar or p.name)
                    for p in Plan.query.all()}
    plan_lookup[0] = "بدون باقة"
    data["revenue"] = {
        "mrr": float(mrr()),
        "arr": float(arr()),
        "plan_distribution": [
            {"label": plan_lookup.get(pid, f"#{pid}"),
             "count": count}
            for pid, count in plan_distribution().items()
        ],
        "subscription_states": subscription_states(),
        "renewals_7d": renewals_due(days=7),
        "renewals_30d": renewals_due(days=30),
        "monthly_series": monthly_revenue_series(months=12),
    }
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
    # MARSOUD-BOT-PROTECTION-01 (Abdelhamid 2026-07-24) — hide
    # unverified-owner companies by default so bot signups don't
    # clutter the list. Toggle with ?show_unverified=1 to review.
    show_unverified = request.args.get("show_unverified") == "1"
    unverified_count = sum(1 for r in rows if not r["owner_verified"])
    if not show_unverified:
        rows = [r for r in rows if r["owner_verified"]]
    sort = (request.args.get("sort") or "").strip()
    from datetime import datetime
    _min = datetime.min
    if sort == "activity":
        rows.sort(key=lambda r: r.get("last_activity") or _min, reverse=True)
    elif sort == "created_asc":
        rows.sort(key=lambda r: r["company"].created_at or _min)
    elif sort == "created_desc":
        rows.sort(key=lambda r: r["company"].created_at or _min, reverse=True)
    return render_template("admin/companies.html", rows=rows, q=q, sort=sort,
                             show_unverified=show_unverified,
                             unverified_count=unverified_count)


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
    # MARSOUD-SUPERADMIN-LINK-USER-01 (Batch 7 Ticket 1) — expose
    # the roles list to the link-user form. Skip `client` +
    # `employee` (attached via portal signup / HR self-service).
    from app.services.permissions import ALL_ROLES, ROLE_LABELS_AR
    linkable_roles = [r for r in ALL_ROLES
                       if r not in ("client", "employee")]

    # MARSOUD-SUPERADMIN-CONTROL-01 T6 (2026-08-08) — Company 360°.
    # Compose the extra panels. Each composer is guarded so a
    # crash in one section doesn't blank the whole page.
    from app.services.company_360 import (
        subscription_snapshot, usage_snapshot, ai_usage_row,
        owners_of, module_matrix, errors_preview,
    )

    def _safe(fn, fallback):
        try:
            return fn()
        except Exception:
            current_app.logger.exception(
                "company_360 composer failed for company_id=%s",
                company_id)
            return fallback

    subscription = _safe(lambda: subscription_snapshot(company), None)
    usage_cards = _safe(lambda: usage_snapshot(company), [])
    ai_row = _safe(lambda: ai_usage_row(company), None)
    owners = _safe(lambda: owners_of(company), [])
    modules = _safe(lambda: module_matrix(company), [])
    errors = _safe(lambda: errors_preview(company, limit=10), [])

    return render_template("admin/company_detail.html",
                           company=company,
                           company_users=company_users,
                           recent_activity=recent_activity,
                           plans=plans,
                           linkable_roles=linkable_roles,
                           role_labels_ar=ROLE_LABELS_AR,
                           subscription=subscription,
                           usage_cards=usage_cards,
                           ai_row=ai_row,
                           owners=owners,
                           modules=modules,
                           errors=errors)


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
        # MARSOUD-PLAN-SSOT — the `plan` form field was writing to
        # Company.plan (a legacy String column, defaulted to "FREE")
        # which was the primary source of the "FREE" label leaking into
        # the super-admin UI. The plan-switcher on the company detail
        # page (POSTs to `companies_assign_plan` and updates plan_id)
        # is now the ONLY supported way to change a company's plan.
        # We deliberately ignore any `plan` field on this form.
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


# ── MARSOUD-COMPANIES-BULK-DELETE (2026-08-12) ───────────── #
# Three routes:
#  · POST /companies/bulk-soft-delete — mark N companies as
#    soft-deleted in one submit from the main table.
#  · GET  /companies/deleted — dedicated page listing every
#    soft-deleted company with a bulk hard-delete toolbar.
#  · POST /companies/bulk-hard-delete — permanent wipe. A
#    JSON snapshot of every row referencing the company is
#    dumped to app/static/backups/company_purges/ BEFORE
#    the cascade runs, so a mistaken purge is still
#    recoverable from disk.
@bp.route("/companies/bulk-soft-delete", methods=["POST"])
@login_required
@superadmin_required
def companies_bulk_soft_delete():
    """Bulk soft-delete. Reads `company_id` (multi) + optional
    `reason` from the form. Each row goes through the same
    `soft_delete_company` helper the per-row route uses, so
    audit + is_active semantics stay identical."""
    from app.services.lifecycle import soft_delete_company
    ids = [int(x) for x in request.form.getlist("company_id") if x]
    reason = ((request.form.get("reason") or "").strip()
               or "bulk soft-delete")
    if not ids:
        flash("لم يتم تحديد أي شركة", "error")
        return redirect(url_for("superadmin.companies"))
    ok = err = 0
    for cid in ids:
        c = db.session.get(Company, cid)
        if c is None:
            err += 1
            continue
        try:
            soft_delete_company(c, actor_id=current_user.id,
                                 reason=reason)
            ok += 1
        except Exception:  # noqa: BLE001
            db.session.rollback()
            err += 1
    if ok and not err:
        flash(f"تم حذف {ok} شركة مؤقتاً", "success")
    elif ok and err:
        flash(f"تم حذف {ok} شركة مؤقتاً — فشل {err}",
              "warning")
    else:
        flash("لم يُحذف أي شركة", "error")
    return redirect(url_for("superadmin.companies"))


@bp.route("/companies/deleted")
@login_required
@superadmin_required
def companies_deleted():
    """List every soft-deleted company. Bulk toolbar sends
    the ticked ids to companies_bulk_hard_delete."""
    from app.services.superadmin import companies_with_stats
    all_rows = companies_with_stats(include_deleted=True)
    rows = [r for r in all_rows
            if r["company"].deleted_at is not None]
    return render_template("admin/companies_deleted.html",
                            rows=rows)


@bp.route("/companies/bulk-hard-delete", methods=["POST"])
@login_required
@superadmin_required
def companies_bulk_hard_delete():
    """Permanent wipe, gated by a typed confirmation. For each
    ticked company: (1) dump a JSON snapshot to disk, (2)
    fire hard_delete_company. Per-row try/except so one
    broken cascade doesn't abort the batch."""
    from app.services.lifecycle import hard_delete_company
    from app.services.company_purge_backup import (
        dump_company_to_json,
    )
    if (request.form.get("confirm_word") or "").strip() != "تأكيد":
        flash("لم تكتب كلمة التأكيد الصحيحة", "error")
        return redirect(url_for("superadmin.companies_deleted"))
    ids = [int(x) for x in request.form.getlist("company_id") if x]
    reason = ((request.form.get("reason") or "").strip()
               or "bulk hard-delete")
    if not ids:
        flash("لم يتم تحديد أي شركة", "error")
        return redirect(url_for("superadmin.companies_deleted"))
    ok = err = 0
    errors = []
    for cid in ids:
        c = db.session.get(Company, cid)
        if c is None:
            err += 1
            errors.append(f"#{cid}: not found")
            continue
        # 1) snapshot to disk BEFORE the destructive commit.
        try:
            path = dump_company_to_json(cid)
        except Exception as e:  # noqa: BLE001 — surface, don't wipe
            err += 1
            errors.append(f"#{cid}: backup failed — "
                           f"{type(e).__name__}")
            continue
        # 2) fire the existing cascade helper.
        try:
            hard_delete_company(
                c, actor_id=current_user.id,
                reason=f"{reason} (backup={path.name})")
            ok += 1
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            err += 1
            errors.append(f"#{cid}: cascade — "
                           f"{type(e).__name__}")
    if ok:
        flash(f"تم الحذف النهائي لـ {ok} شركة", "success")
    if err:
        flash(f"فشل {err} شركة — " + " · ".join(errors),
              "error")
    return redirect(url_for("superadmin.companies_deleted"))


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


# ── MARSOUD-RESTRICTED-SUPERADMIN-CREATE-UI (2026-08-13) ── #
# Follow-up on MARSOUD-APPROVAL-GATED-SUPERADMIN. Two routes:
#  · GET  /admin/users/create-restricted — the form.
#  · POST /admin/users/create-restricted — promote-or-create.
# Behavior mirrors the shipped `make_superadmin.py` script
# (email exists → flip flags, don't touch other fields;
# email missing → new User with name + password) but via the
# admin UI so a shell is no longer required. Only the primary
# superadmin (requires_approval=False) can use it — a
# restricted user gets 403 in the view body on top of the
# fail-safe from DESTRUCTIVE_ENDPOINTS registration.
@bp.route("/users/create-restricted", methods=["GET"])
@login_required
@superadmin_required
def user_create_restricted_form():
    if getattr(current_user, "requires_approval", False):
        abort(403)
    return render_template(
        "admin/users_create_restricted.html")


@bp.route("/users/create-restricted", methods=["POST"])
@login_required
@superadmin_required
def user_create_restricted():
    from app.services.password_policy import validate_password
    from app.models import UserStatus
    if getattr(current_user, "requires_approval", False):
        abort(403)

    email = (request.form.get("email") or "").strip().lower()
    if not email or "@" not in email:
        flash("أدخل بريدًا إلكترونيًا صحيحًا", "error")
        return redirect(url_for(
            "superadmin.user_create_restricted_form"))

    existing = User.query.filter_by(email=email).first()

    if existing:
        # PROMOTE — flip the two flags, leave everything
        # else untouched. Least-surprise for an existing
        # employee being elevated (no forced re-login).
        if (existing.is_superadmin
                and existing.requires_approval):
            flash(
                f"المستخدم {email} مسؤول مقيَّد بالفعل",
                "info")
            return redirect(url_for("superadmin.users"))
        existing.is_superadmin = True
        existing.requires_approval = True
        existing.is_active = True
        db.session.commit()
        log_platform_action(
            "user_promoted_to_restricted_superadmin",
            target_user_id=existing.id,
            details=f"email={email}",
        )
        flash(f"تم ترقية {email} إلى مسؤول مقيَّد",
              "success")
        return redirect(url_for("superadmin.users"))

    # CREATE — name + password required for a brand-new
    # user. Password validated via the shared policy so
    # signup + reset + this all agree on the same rules.
    full_name = (request.form.get("full_name") or "").strip()
    password = request.form.get("password") or ""
    if not full_name:
        flash("الاسم الكامل مطلوب لمستخدم جديد", "error")
        return redirect(url_for(
            "superadmin.user_create_restricted_form"))
    ok, reason = validate_password(password)
    if not ok:
        flash(reason, "error")
        return redirect(url_for(
            "superadmin.user_create_restricted_form"))

    u = User(
        email=email, full_name=full_name,
        is_superadmin=True, requires_approval=True,
        is_active=True,
        status=UserStatus.ACTIVE.value,
    )
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    log_platform_action(
        "user_created_restricted_superadmin",
        target_user_id=u.id,
        details=f"email={email}",
    )
    flash(f"تم إنشاء المسؤول المقيَّد: {email}", "success")
    return redirect(url_for("superadmin.users"))


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


# ─── MARSOUD-SUPERADMIN-LINK-USER-01 (Batch 7 Ticket 1, 2026-07-29) ───
@bp.route("/companies/<int:company_id>/link-user", methods=["POST"])
@login_required
@superadmin_required
def company_link_user(company_id):
    """Attach an email to a company from super-admin. Two paths:

      · Existing User → INSERT (or UPDATE) user_companies row with
        the chosen role. Bypasses the regular /users/invite
        `role != 'owner'` guard — super-admin is the escape hatch
        when a company loses its only owner.
      · New email → CREATE Invitation + send accept email. Reuses
        the same helpers the regular owner-invite flow uses so
        the accept URL + email template stay consistent.

    Multiple owners are permitted (schema allows it — PK is
    user_id + company_id, no unique on role). Cross-tenant is
    enforced by the (company_id, user_id) primary key.
    """
    from app.services.permissions import ALL_ROLES, ROLE_LABELS_AR
    from app.models import Role
    from app.services.email import send_invitation_email
    from app.services.permissions import generate_invite_token

    company = db.session.get(Company, company_id) or _404()
    email = (request.form.get("email") or "").strip().lower()
    role = (request.form.get("role") or "").strip()

    # Basic input validation.
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        flash("بريد إلكتروني غير صالح", "error")
        return redirect(url_for("superadmin.company_detail",
                                 company_id=company_id))
    if role not in ALL_ROLES:
        flash("دور غير صالح", "error")
        return redirect(url_for("superadmin.company_detail",
                                 company_id=company_id))

    # Existing-user path.
    existing = User.query.filter_by(email=email).first()
    if existing:
        # Refuse to link deactivated / soft-deleted accounts.
        if not existing.is_active:
            flash("المستخدم موقوف — فعّل الحساب أولاً", "error")
            return redirect(url_for("superadmin.company_detail",
                                     company_id=company_id))
        # Look up the matching Role row (some deployments have
        # per-company custom Role rows; system role is our
        # fallback).
        role_row = Role.query.filter_by(
            company_id=company_id, code=role).first()
        # Check for existing user_companies row (re-link case:
        # super-admin may be re-adding the same user right after
        # an unlink).
        row = db.session.execute(
            user_companies.select().where(
                (user_companies.c.user_id == existing.id) &
                (user_companies.c.company_id == company_id)
            )
        ).fetchone()
        if row:
            db.session.execute(
                user_companies.update()
                .where(
                    (user_companies.c.user_id == existing.id) &
                    (user_companies.c.company_id == company_id)
                )
                .values(role=role,
                         role_id=role_row.id if role_row else None)
            )
            action = "user_link_role_updated"
            msg = (f"تم تحديث دور {existing.email} إلى "
                    f"{ROLE_LABELS_AR.get(role, role)}")
        else:
            db.session.execute(user_companies.insert().values(
                user_id=existing.id, company_id=company_id,
                role=role,
                role_id=role_row.id if role_row else None,
            ))
            action = ("user_link_owner_by_superadmin"
                       if role == "owner" else "user_link_to_company")
            msg = (f"تم ربط {existing.email} بالشركة كـ "
                    f"{ROLE_LABELS_AR.get(role, role)}")
        db.session.commit()
        log_platform_action(action,
                             target_user_id=existing.id,
                             target_company_id=company_id)
        flash(msg, "success")
        return redirect(url_for("superadmin.company_detail",
                                 company_id=company_id))

    # New-email path — mint an invitation + send accept email.
    token = generate_invite_token({
        "email": email, "company_id": company_id, "role": role,
    })
    inv = Invitation(
        company_id=company_id,
        email=email, role=role, token=token,
        invited_by_id=current_user.id,
    )
    db.session.add(inv)
    db.session.commit()
    accept_url = url_for("invitations.accept", token=token,
                          _external=True)
    try:
        send_invitation_email(inv, accept_url)
    except Exception:
        import logging
        logging.getLogger("marsoud.superadmin").exception(
            "invite email send failed for %s", email)
    log_platform_action("user_invite_from_superadmin",
                         target_company_id=company_id)
    flash(f"تم إرسال دعوة إلى {email}", "success")
    return redirect(url_for("superadmin.company_detail",
                             company_id=company_id))


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


# ── MARSOUD-VBILL-STATUS-VISIBILITY (2026-08-17) — TKT-D ── #
# Cross-tenant view of every overdue supplier bill in the platform.
# Uses the same vendor_bill_bucket helper the tenant dashboard +
# list use, so the super-admin never sees a different picture than
# the company itself. Click-through links go to the tenant's own
# bill view (behind impersonation to keep the tenant-scoped route
# working).
@bp.route("/vendor-bills/overdue")
@login_required
@superadmin_required
def vendor_bills_overdue():
    from app.services.superadmin import overdue_vendor_bills_by_company
    rows = overdue_vendor_bills_by_company()
    return render_template(
        "admin/vendor_bills_overdue.html",
        rows=rows,
        total_companies=len(rows),
        total_amount=sum(r["total_amount"] for r in rows),
        total_bills=sum(len(r["bills"]) for r in rows),
    )


# ── MARSOUD-SUPERADMIN-USER-360 (2026-08-17) — User Detail page. ─────────── #
# One-shot view of everything the super-admin might want to see about a
# specific user: basic account fields, all companies they belong to (with
# per-company plan snapshot), roles + granted permissions, recent activity
# log, session/login history, invitations tied to their email, and
# consent-event history. The existing per-action endpoints
# (user_toggle / user_reset_password / user_unlink / user_resend_invite /
# user_consent) stay as the write endpoints — this page just renders and
# links to them.
@bp.route("/users/<int:user_id>")
@login_required
@superadmin_required
def user_detail(user_id):
    from app.services.user_360 import user_snapshot
    snap = user_snapshot(user_id)
    if snap is None:
        abort(404)
    return render_template("admin/user_detail.html", snap=snap)


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


# ── MARSOUD-SUPERADMIN-CONTROL-01 T7 (2026-08-08) — AI Control Center ── #
@bp.route("/ai-control", methods=["GET", "POST"])
@login_required
@superadmin_required
def ai_control():
    """Single hub for every AI knob: provider key status, model
    routing per persona, fallback order, global caps (max_tokens
    + kill switch), and a filterable turn log. Replaces the
    sidebar entries for /admin/ai-usage + /admin/ai-settings;
    those two URLs still work and link back here."""
    from app.services.ai_control import (
        providers_status, model_routing, fallback_order,
        global_caps, turn_log,
        set_fallback_order, set_max_tokens, set_globally_disabled,
        set_model_routing, KNOWN_PROVIDERS,
    )

    if request.method == "POST":
        section = (request.form.get("section") or "").strip()
        try:
            if section == "model_routing":
                set_model_routing(
                    persona=request.form.get("persona"),
                    provider=request.form.get("provider"),
                    model=(request.form.get("model") or "").strip(),
                    actor_id=current_user.id)
            elif section == "fallback":
                raw = (request.form.get("order") or "").strip()
                order = [x.strip() for x in raw.split(",") if x.strip()]
                set_fallback_order(order, actor_id=current_user.id)
            elif section == "max_tokens":
                set_max_tokens(request.form.get("value") or 0,
                                actor_id=current_user.id)
            elif section == "kill_switch":
                flag = (request.form.get("value") == "on")
                set_globally_disabled(flag, actor_id=current_user.id)
            else:
                flash("قسم غير معروف", "error")
                return redirect(url_for("superadmin.ai_control"))
            flash("💾 تم حفظ الإعدادات", "success")
        except ValueError as e:
            flash(f"خطأ: {e}", "error")
        return redirect(url_for("superadmin.ai_control"))

    args = request.args

    def _int_or_none(v):
        return int(v) if (v or "").isdigit() else None

    return render_template(
        "admin/ai_control.html",
        providers=providers_status(),
        routing=model_routing(),
        fallback=fallback_order(),
        caps=global_caps(),
        turns=turn_log(
            company_id=_int_or_none(args.get("company_id")),
            user_id=_int_or_none(args.get("user_id")),
            provider=(args.get("provider") or None),
            hours=int(args["hours"]) if args.get("hours", "").isdigit() else 24,
        ),
        turn_filters={k: v for k, v in args.items()},
        KNOWN_PROVIDERS=KNOWN_PROVIDERS,
    )
# ── MARSOUD-SUPERADMIN-CONTROL-01 T10 (2026-08-08) — Ctrl+K palette ── #
@bp.route("/nav-search.json")
@login_required
@superadmin_required
def nav_search_json():
    """Backing endpoint for the Ctrl+K palette overlay defined in
    templates/admin/base.html. Returns grouped results (nav /
    companies / users) filtered by ?q=…."""
    from flask import jsonify
    from app.services.nav_search import search_all
    q = (request.args.get("q") or "").strip()
    return jsonify(search_all(q))
# ── MARSOUD-SUPERADMIN-CONTROL-01 T11 (2026-08-08) — Ops & Health ── #
@bp.route("/ops-health")
@login_required
@superadmin_required
def ops_health():
    """Single-screen operational dashboard: vitals + errors 24h +
    cron last-runs + DB stats + audit tail. Meta-refresh every
    15s in the template. Each composer is guarded so a failure
    in one card never blanks the page."""
    from app.services.ops_health import (
        system_vitals, errors_summary, cron_last_runs,
        db_stats, audit_tail,
    )

    def _safe(fn, fb):
        try:
            return fn()
        except Exception:
            current_app.logger.exception("ops_health composer failed")
            return fb

    return render_template(
        "admin/ops_health.html",
        vitals=_safe(system_vitals, None),
        errors=_safe(lambda: errors_summary(24),
                      {"total": 0, "by_route": [], "by_status": [],
                       "newest": [], "hours": 24}),
        cron=_safe(cron_last_runs, []),
        db_stats=_safe(db_stats, None),
        audit=_safe(lambda: audit_tail(20), []),
    )


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
# MARSOUD-SUPERADMIN-CONTROL-01 T1 (2026-08-08) — ALL_MODULES +
# MODULE_LABELS_AR were hand-maintained here with 11 entries,
# missing insights / cash_custody / evaluations (which PLAN_SEED
# was already writing into Pro plans). Result: those 3 modules
# were literally unpickable in the plan-edit form. Now derived
# from feature_registry so any future module addition shows up
# on the very next page load with zero code change here.
def _all_modules_from_registry():
    from app.services.feature_registry import all_modules
    codes = [m.code for m in all_modules()]
    labels = {m.code: m.label_ar for m in all_modules()}
    return codes, labels


ALL_MODULES, MODULE_LABELS_AR = _all_modules_from_registry()


def _read_subitems_form():
    """MARSOUD-58 — parse selected sub-items from the plan form. Empty
    list = "lock everything"; missing key = legacy mode (NULL = all)."""
    return request.form.getlist("subitems")


def _upsert_plan_price(plan, currency, monthly_raw, yearly_raw):
    """MARSOUD-MULTI-CURRENCY-PRICING — helper used by the plans
    builder. Upserts a plan_prices row per currency. Empty inputs
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


def _upsert_plan_quota(plan, quota_type, *, included_raw,
                        mode_raw, price_extra_raw):
    """MARSOUD-SUPERADMIN-CONTROL-01 T3 (2026-08-08) — mirrors
    _upsert_plan_price: all-blank row = delete; else validate +
    upsert. Bad input is silently skipped (validation UX belongs
    on T5's standalone editor; here the aim is idempotent whole-
    form save)."""
    from app.models import Quota, ENFORCEMENT_MODES
    included = (included_raw or "").strip()
    mode = (mode_raw or "").strip()
    price_extra = (price_extra_raw or "").strip()

    if not included and not mode and not price_extra:
        Quota.query.filter_by(
            plan_id=plan.id, quota_type=quota_type).delete()
        return

    try:
        included_val = int(included) if included else 0
    except ValueError:
        return   # skip bad ints silently
    if included_val < 0:
        return
    if mode and mode not in ENFORCEMENT_MODES:
        return
    try:
        price_val = float(price_extra) if price_extra else None
    except ValueError:
        price_val = None

    row = Quota.query.filter_by(
        plan_id=plan.id, quota_type=quota_type).first()
    if not row:
        row = Quota(plan_id=plan.id, quota_type=quota_type)
        db.session.add(row)
    row.included_amount = included_val
    if mode:
        row.enforcement_mode = mode
    row.price_per_extra_unit = price_val


# ── MARSOUD-SUPERADMIN-CONTROL-01 T3 (2026-08-08) — Plan Builder ── #
@bp.route("/plans", methods=["GET"])
@login_required
@superadmin_required
def plans_index():
    """Two-pane builder. Right-side (RTL) plan list; left-side
    detail form for the selected plan. Selection via
    ?plan_id=<id|new>; empty selects the first plan when any
    exist. Same URL as before; old bookmarks stay live via the
    plans_new / plans_edit redirects below."""
    from app.services.plan_gating import (
        SUB_ITEM_CATALOG, SECTION_LABEL_AR, SECTION_REQUIRES_MODULES,
    )
    from app.models import (
        PlanPrice, Quota, KNOWN_QUOTA_TYPES, ENFORCEMENT_MODES,
    )
    plans = Plan.query.order_by(Plan.id).all()
    counts = {p.id: Company.query.filter_by(plan_id=p.id).count()
               for p in plans}
    intended_counts = {
        p.id: Company.query.filter_by(intended_plan_id=p.id).count()
        for p in plans}

    sel = (request.args.get("plan_id") or "").strip()
    current = None
    if sel == "new":
        mode = "create"
    elif sel.isdigit():
        current = db.session.get(Plan, int(sel))
        mode = "edit" if current else "empty"
    elif plans:
        current = plans[0]
        mode = "edit"
    else:
        mode = "empty"

    current_quotas = {qt: None for qt in KNOWN_QUOTA_TYPES}
    sar_price = None
    if current:
        for q in Quota.query.filter_by(plan_id=current.id).all():
            if q.quota_type in current_quotas:
                current_quotas[q.quota_type] = q
        sar_price = PlanPrice.query.filter_by(
            plan_id=current.id, currency="SAR").first()

    return render_template(
        "admin/plans_index.html",
        plans=plans, counts=counts,
        intended_counts=intended_counts,
        current=current, mode=mode,
        current_quotas=current_quotas,
        sar_price=sar_price,
        all_modules=ALL_MODULES,
        module_labels=MODULE_LABELS_AR,
        sub_item_catalog=SUB_ITEM_CATALOG,
        section_label_ar=SECTION_LABEL_AR,
        section_requires_modules=SECTION_REQUIRES_MODULES,
        known_quota_types=KNOWN_QUOTA_TYPES,
        enforcement_modes=ENFORCEMENT_MODES,
    )


@bp.route("/plans/save", methods=["POST"])
@login_required
@superadmin_required
def plans_save():
    """Unified create + update. Reads hidden plan_id; blank ⇒
    create. Handles identity + EGP/SAR prices + modules +
    subitems + inline quotas in one transaction. Redirects to
    the builder with the saved plan preselected."""
    from app.models import KNOWN_QUOTA_TYPES
    pid = (request.form.get("plan_id") or "").strip()
    creating = not pid.isdigit()

    if creating:
        code = (request.form.get("code") or "").strip().lower()
        if not code:
            flash("الكود مطلوب", "error")
            return redirect(url_for("superadmin.plans_index",
                                     plan_id="new"))
        if Plan.query.filter_by(code=code).first():
            flash(f"الكود {code} مستخدم بالفعل", "error")
            return redirect(url_for("superadmin.plans_index",
                                     plan_id="new"))
        p = Plan(code=code)
        db.session.add(p)
    else:
        p = db.session.get(Plan, int(pid)) or _404()

    # Identity
    p.name = (request.form.get("name") or p.name or "").strip()
    p.name_ar = (request.form.get("name_ar") or p.name_ar or "").strip()
    p.description = (request.form.get("description") or "").strip() or None
    if not creating:
        p.is_active = (request.form.get("is_active") == "on")

    # Legacy EGP columns (base currency).
    def _num(name):
        raw = (request.form.get(name) or "").strip()
        try:
            return float(raw) if raw else None
        except ValueError:
            return None
    p.price_monthly = _num("price_monthly")
    p.price_yearly = _num("price_yearly")

    # Modules; subitems only when the form's explicit marker is
    # present (else leave NULL = all-on legacy behaviour).
    p.set_modules(request.form.getlist("modules"))
    if request.form.get("submit_subitems") == "1":
        p.set_subitems(_read_subitems_form())

    db.session.flush()   # need p.id for child upserts

    _upsert_plan_price(
        p, "SAR",
        request.form.get("price_monthly_sar"),
        request.form.get("price_yearly_sar"))

    for qt in KNOWN_QUOTA_TYPES:
        _upsert_plan_quota(
            p, qt,
            included_raw=request.form.get(f"included_{qt}"),
            mode_raw=request.form.get(f"enforcement_{qt}"),
            price_extra_raw=request.form.get(f"price_extra_{qt}"),
        )

    db.session.commit()
    log_platform_action(
        "plan_create" if creating else "plan_edit",
        actor_id=current_user.id,
        details=f"code={p.code} name={p.name_ar or p.name}")
    flash("💾 تم حفظ الباقة", "success")
    return redirect(url_for("superadmin.plans_index",
                             plan_id=p.id))


@bp.route("/plans/<int:plan_id>/delete", methods=["POST"])
@login_required
@superadmin_required
def plans_delete(plan_id):
    """In-use guarded delete. Refuses when any company references
    the plan (plan_id OR intended_plan_id). Scrubs coupon JSON,
    wipes child Quota + PlanPrice rows (SQLite doesn't enforce
    ondelete=CASCADE without PRAGMA foreign_keys=ON), then
    removes the plan."""
    from app.models import Coupon, Quota, PlanPrice
    p = db.session.get(Plan, plan_id) or _404()
    used_count = Company.query.filter_by(plan_id=p.id).count()
    intended_count = Company.query.filter_by(
        intended_plan_id=p.id).count()
    if used_count or intended_count:
        flash(
            f"لا يمكن حذف الباقة — مربوطة بـ {used_count} شركة "
            f"و {intended_count} كنيّة اشتراك. أوقف الباقة أو "
            f"انقل الشركات لباقة أخرى أولاً.",
            "error")
        return redirect(url_for("superadmin.plans_index",
                                 plan_id=p.id))

    # Scrub coupon JSON — no FK to enforce this.
    for c in Coupon.query.all():
        ids = c.plan_ids or []
        if p.id in ids:
            c.set_plan_ids([i for i in ids if i != p.id])

    Quota.query.filter_by(plan_id=p.id).delete()
    PlanPrice.query.filter_by(plan_id=p.id).delete()

    code = p.code
    db.session.delete(p)
    db.session.commit()
    log_platform_action(
        "plan_delete",
        actor_id=current_user.id,
        details=f"code={code}")
    flash(f"🗑 تم حذف الباقة {code}", "success")
    return redirect(url_for("superadmin.plans_index"))


# Legacy URLs — old bookmarks / docs stay live via redirects.
@bp.route("/plans/new", methods=["GET"])
@login_required
@superadmin_required
def plans_new():
    return redirect(url_for("superadmin.plans_index", plan_id="new"))


@bp.route("/plans/<int:plan_id>/edit", methods=["GET"])
@login_required
@superadmin_required
def plans_edit(plan_id):
    return redirect(url_for("superadmin.plans_index",
                             plan_id=plan_id))


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


# ── MARSOUD-SIGNUP-AUTO-BLOCK (2026-08-12) ──────────────── #
# Two routes for the auto-learning blocklist review:
#  · GET  /admin/rejected-signups — inbox listing last 200
#    rejections + every actively-blocked domain.
#  · POST /admin/rejected-signups/unblock — human review
#    escape hatch (lifts an auto-block that turned out to
#    be a false positive).
@bp.route("/rejected-signups")
@login_required
@superadmin_required
def rejected_signups():
    """MARSOUD-SIGNUP-AUTO-BLOCK inbox — was silently rendering
    an empty "لا توجد محاولات" state whenever the underlying
    query broke (e.g. the signup_rejections migration hadn't run
    on prod). Now every failure mode surfaces as a clear
    diagnostic banner instead of hiding.

    MARSOUD-BOT-REGISTRATION-VISIBILITY (2026-08-17) — TKT-17.
    Also queries the per-email blocklist so the super-admin sees
    the emails that tripped the honeypot alongside the domains.

    Detection order:
      1. Both tables missing → migration not applied on this DB.
      2. Query raises → wrap and log the exception, surface the
         message on the page so it's obvious the panel is broken,
         not just quiet.
    """
    import sqlalchemy as sa
    import logging
    from app.models import SignupRejection, BlockedDomain, BlockedEmail
    from app.services.signup_rejections import (
        WHITELISTED_DOMAINS, HONEYPOT_TRIGGER_COUNT,
        HONEYPOT_TRIGGER_WINDOW_HOURS,
    )

    _log = logging.getLogger("marsoud.rejected_signups")
    error = None
    rejections = []
    blocked = []
    blocked_emails = []

    # Cheap schema probe — if the tables don't exist, the migration
    # never ran and we should say so instead of showing an empty
    # panel that looks like "everything is fine".
    try:
        insp = sa.inspect(db.engine)
        tables = set(insp.get_table_names())
        missing = [t for t in ("signup_rejections", "blocked_domains", "blocked_emails")
                   if t not in tables]
        if missing:
            error = (
                "الجداول التالية غير موجودة في قاعدة البيانات: "
                + ", ".join(missing)
                + ". شغّل `flask db upgrade` على السيرفر — كل شكاوى "
                + "التسجيل بتُرمَى ذاكرةً حتى يوجد الجدول."
            )
    except Exception:
        _log.exception("schema inspect failed")

    if not error:
        try:
            rejections = (SignupRejection.query
                           .order_by(SignupRejection.created_at.desc())
                           .limit(200).all())
        except Exception as e:
            _log.exception("SignupRejection query failed")
            error = f"فشل تحميل سجل الرفض: {e}"

        try:
            blocked = (BlockedDomain.query
                        .filter_by(is_active=True)
                        .order_by(BlockedDomain.blocked_at.desc())
                        .all())
        except Exception as e:
            _log.exception("BlockedDomain query failed")
            error = (error or "") + f" · فشل تحميل الدومينز المحظورة: {e}"

        try:
            blocked_emails = (BlockedEmail.query
                               .filter_by(is_active=True)
                               .order_by(BlockedEmail.blocked_at.desc())
                               .all())
        except Exception as e:
            _log.exception("BlockedEmail query failed")
            error = (error or "") + f" · فشل تحميل الإيميلات المحظورة: {e}"

    return render_template(
        "admin/rejected_signups.html",
        rejections=rejections, blocked=blocked,
        blocked_emails=blocked_emails,
        error=error,
        whitelist=sorted(WHITELISTED_DOMAINS),
        trigger_count=HONEYPOT_TRIGGER_COUNT,
        trigger_window_hours=HONEYPOT_TRIGGER_WINDOW_HOURS,
    )


@bp.route("/rejected-signups/unblock", methods=["POST"])
@login_required
@superadmin_required
def rejected_signups_unblock():
    from app.services.signup_rejections import unblock_domain
    domain = (request.form.get("domain") or "").strip().lower()
    if not domain:
        flash("لم يتم تحديد دومين", "error")
    elif unblock_domain(domain, actor_id=current_user.id):
        flash(f"تم فك الحظر عن {domain}", "success")
    else:
        flash("الدومين ليس محظوراً حالياً", "info")
    return redirect(url_for("superadmin.rejected_signups"))


# MARSOUD-BOT-REGISTRATION-VISIBILITY (2026-08-17) — TKT-17.
# Sibling of rejected_signups_unblock for the per-email blocklist.
@bp.route("/rejected-signups/unblock-email", methods=["POST"])
@login_required
@superadmin_required
def rejected_signups_unblock_email():
    from app.services.signup_rejections import unblock_email
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        flash("لم يتم تحديد بريد إلكتروني", "error")
    elif unblock_email(email, actor_id=current_user.id):
        flash(f"تم فك الحظر عن {email}", "success")
    else:
        flash("البريد ليس محظوراً حالياً", "info")
    return redirect(url_for("superadmin.rejected_signups"))


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


# ─── MARSOUD-SUPERADMIN-CONTROL-01 T4 (2026-08-08) — company overrides
@bp.route("/overrides", methods=["GET", "POST"])
@login_required
@superadmin_required
def overrides_index():
    """Central admin page for per-company feature grant/deny.
    GET renders the table + add-form; POST creates a new override."""
    from datetime import datetime
    from app.services.company_overrides import (
        upsert_override, list_all,
    )
    from app.services.feature_registry import all_modules

    if request.method == "POST":
        try:
            company_id = int(request.form.get("company_id") or 0)
            # MARSOUD-SUBITEM-OVERRIDES (2026-08-09) — the add-form
            # ships two feature selects (module list + grouped
            # subitem list) hidden/shown by the scope radio. Read
            # whichever matches the submitted scope; default to
            # MODULE for pre-ticket callers/bookmarks.
            scope = (request.form.get("scope") or "MODULE").strip().upper()
            if scope == "SUBITEM":
                feature_code = (
                    request.form.get("feature_code_subitem")
                    or request.form.get("feature_code") or "").strip()
            else:
                feature_code = (
                    request.form.get("feature_code_module")
                    or request.form.get("feature_code") or "").strip()
            mode = (request.form.get("mode") or "").strip()
            reason = (request.form.get("reason") or "").strip()
            exp_raw = (request.form.get("expires_at") or "").strip()
            expires_at = None
            if exp_raw:
                # HTML date input → YYYY-MM-DD. End-of-day so the
                # override is active for the whole picked day.
                expires_at = datetime.strptime(exp_raw, "%Y-%m-%d")
                expires_at = expires_at.replace(hour=23, minute=59)
            if not company_id:
                raise ValueError("اختر الشركة")
            upsert_override(
                company_id, feature_code, mode, reason,
                expires_at=expires_at, actor_id=current_user.id,
                scope=scope,
            )
            flash(
                f"✅ تم تسجيل استثناء ({mode}) على {feature_code}",
                "success",
            )
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for(
            "superadmin.overrides_index",
            company_id=request.form.get("company_id") or None,
        ))

    # GET — build the filtered list.
    filter_company_id = request.args.get("company_id", type=int)
    filter_mode = request.args.get("mode") or None
    filter_status = request.args.get("status") or "active"
    rows = list_all(
        company_id=filter_company_id, mode=filter_mode,
        status=filter_status,
    )
    # For the add-form + filter selects.
    companies = Company.query.filter_by(is_active=True)\
        .order_by(Company.name).all()
    modules = all_modules()
    # MARSOUD-SUBITEM-OVERRIDES (2026-08-09) — the subitem picker
    # renders the same catalogue the /admin/plans editor uses,
    # grouped by section so the admin scans by area (Sales,
    # Inventory, HR, …) instead of a flat 40-item list.
    from app.services.plan_gating import (
        SUB_ITEM_CATALOG, SECTION_LABEL_AR,
    )
    subitems_by_section = list(SUB_ITEM_CATALOG.items())
    return render_template(
        "admin/overrides_index.html",
        rows=rows, companies=companies, modules=modules,
        filter_company_id=filter_company_id,
        filter_mode=filter_mode,
        filter_status=filter_status,
        subitems_by_section=subitems_by_section,
        section_labels=SECTION_LABEL_AR,
    )


@bp.route("/overrides/<int:override_id>/revoke", methods=["POST"])
@login_required
@superadmin_required
def overrides_revoke(override_id):
    """Delete an override. Audit trail preserved in
    platform_audit_logs via revoke_override's log call."""
    from app.services.company_overrides import revoke_override
    ok = revoke_override(override_id, actor_id=current_user.id)
    flash("🗑️ تم إلغاء الاستثناء" if ok
          else "الاستثناء غير موجود", "success" if ok else "error")
    # Preserve the caller's filter — if they came from a
    # per-company view, land back there.
    company_id = request.args.get("company_id", type=int)
    return redirect(url_for(
        "superadmin.overrides_index",
        company_id=company_id,
    ))


# ─── MARSOUD-SUPERADMIN-CONTROL-01 T8 (2026-08-08) — route coverage
@bp.route("/routes")
@login_required
@superadmin_required
def routes_index():
    """Read-only viewer for the route coverage audit. Same data
    the `flask audit-routes` CLI reports, filterable."""
    from flask import current_app
    from app.services.route_audit import build_coverage, summary
    rows = build_coverage(current_app._get_current_object())
    # Filters
    cat = request.args.get("category") or ""
    mod = request.args.get("module") or ""
    q = (request.args.get("q") or "").strip().lower()

    def _passes(r):
        if cat:
            if cat == "orphan":
                if not r.is_orphan:
                    return False
            elif cat == "ignored":
                if r.ignored_reason is None:
                    return False
            elif r.category != cat:
                return False
        if mod and r.module != mod:
            return False
        if q and q not in r.endpoint.lower() and q not in r.url_rule.lower():
            return False
        return True

    filtered = [r for r in rows if _passes(r)]
    # Module dropdown options — sorted unique
    module_codes = sorted({r.module for r in rows if r.module})
    return render_template(
        "admin/routes_index.html",
        rows=filtered,
        summary=summary(current_app._get_current_object()),
        module_codes=module_codes,
        filter_category=cat, filter_module=mod, filter_q=q,
    )
# ─── MARSOUD-SUPERADMIN-CONTROL-01 T5 (2026-08-08) — quotas admin
@bp.route("/quotas", methods=["GET"])
@login_required
@superadmin_required
def quotas_index():
    """One page: every plan's quotas (inline-editable per row) + a
    consumption panel showing companies sorted by worst-percentage.
    Ticket's mandate: super-admin sets limits from the panel + sees
    who's near / over."""
    from app.services.quotas import list_consumption
    from app.models import (
        KNOWN_QUOTA_TYPES, ENFORCEMENT_MODES, Quota,
    )
    plans = Plan.query.filter_by(is_active=True)\
        .order_by(Plan.id).all()
    # rows_by_plan[plan_id][quota_type] → Quota | None. Guarantees
    # every plan renders all 4 quota rows (users / ai_tokens_month /
    # storage_bytes / branches) even when a row hasn't been saved
    # yet — the form shows "not set / save to create".
    # Plan has no `quotas` relationship — query directly to avoid a
    # per-plan lazy load surprise.
    rows_by_plan = {}
    for p in plans:
        existing_rows = Quota.query.filter_by(plan_id=p.id).all()
        existing = {q.quota_type: q for q in existing_rows}
        rows_by_plan[p.id] = {
            qt: existing.get(qt) for qt in KNOWN_QUOTA_TYPES
        }
    threshold = request.args.get("threshold", "0") or "0"
    try:
        threshold_i = max(0, int(threshold))
    except (TypeError, ValueError):
        threshold_i = 0
    consumption = list_consumption(threshold_pct=threshold_i)
    return render_template(
        "admin/quotas_index.html",
        plans=plans, rows_by_plan=rows_by_plan,
        consumption=consumption,
        enforcement_modes=ENFORCEMENT_MODES,
        known_quota_types=KNOWN_QUOTA_TYPES,
        threshold=threshold,
    )


@bp.route("/quotas/plan/<int:plan_id>/save", methods=["POST"])
@login_required
@superadmin_required
def quotas_save(plan_id):
    """Save one quota row (form-per-row pattern from
    feature_flags.html). Refuses unknown quota_type / mode /
    negative amount; flashes the Arabic error."""
    from app.services.quotas import upsert_quota
    plan = db.session.get(Plan, plan_id) or _404()
    quota_type = (request.form.get("quota_type") or "").strip()
    try:
        price_raw = (request.form.get("price_per_extra_unit") or "").strip()
        upsert_quota(
            plan_id=plan.id,
            quota_type=quota_type,
            included_amount=request.form.get("included_amount") or 0,
            enforcement_mode=(request.form.get("enforcement_mode")
                              or "").strip(),
            price_per_extra_unit=float(price_raw) if price_raw else None,
            actor_id=current_user.id,
        )
        flash(f"💾 حُفظ حد {quota_type} على باقة {plan.name_ar}",
              "success")
    except (ValueError, TypeError) as e:
        flash(f"خطأ: {e}", "error")
    return redirect(url_for("superadmin.quotas_index"))


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


# ─── MARSOUD-HELP-CENTER-01 (Abdelhamid 2026-07-24) ──────────────
# Super-admin CRUD for the in-product help articles. Public reads
# live in app/routes/help.py.

@bp.route("/help")
@login_required
@superadmin_required
def help_index():
    from app.models import HelpArticle
    rows = HelpArticle.query.order_by(
        HelpArticle.module_key.asc(),
        HelpArticle.display_order.asc()).all()
    return render_template("admin/help_index.html", rows=rows)


@bp.route("/help/new", methods=["GET", "POST"])
@login_required
@superadmin_required
def help_new():
    from app.models import HelpArticle
    if request.method == "POST":
        a = HelpArticle(
            module_key=(request.form.get("module_key") or "").strip(),
            title_ar=(request.form.get("title_ar") or "").strip(),
            title_en=(request.form.get("title_en") or "").strip() or None,
            goal=(request.form.get("goal") or "").strip() or None,
            general_explanation=(
                request.form.get("general_explanation") or "").strip()
                or None,
            display_order=int(request.form.get("display_order") or 0),
            is_published=(request.form.get("is_published") == "on"),
            created_by_id=current_user.id,
        )
        a.set_tips(_split_lines(request.form.get("tips")))
        a.set_related(_split_lines(request.form.get("related_module_keys")))
        if not a.module_key or not a.title_ar:
            flash("module_key والعنوان مطلوبان", "error")
            return redirect(url_for("superadmin.help_new"))
        db.session.add(a); db.session.commit()
        flash("تم إنشاء المقال. أضف الآن الأمثلة والوسائط.", "success")
        return redirect(url_for("superadmin.help_edit", article_id=a.id))
    return render_template("admin/help_form.html", article=None)


@bp.route("/help/<int:article_id>/edit", methods=["GET", "POST"])
@login_required
@superadmin_required
def help_edit(article_id):
    from app.models import HelpArticle
    a = db.session.get(HelpArticle, article_id) or _404()
    if request.method == "POST":
        a.module_key = (request.form.get("module_key")
                        or a.module_key).strip()
        a.title_ar = (request.form.get("title_ar") or a.title_ar).strip()
        a.title_en = (request.form.get("title_en") or "").strip() or None
        a.goal = (request.form.get("goal") or "").strip() or None
        a.general_explanation = (
            request.form.get("general_explanation") or "").strip() or None
        a.display_order = int(request.form.get("display_order") or 0)
        a.is_published = (request.form.get("is_published") == "on")
        a.set_tips(_split_lines(request.form.get("tips")))
        a.set_related(_split_lines(
            request.form.get("related_module_keys")))
        db.session.commit()
        flash("تم الحفظ", "success")
        return redirect(url_for("superadmin.help_edit",
                                 article_id=a.id))
    return render_template("admin/help_form.html", article=a)


@bp.route("/help/<int:article_id>/toggle", methods=["POST"])
@login_required
@superadmin_required
def help_toggle(article_id):
    from app.models import HelpArticle
    a = db.session.get(HelpArticle, article_id) or _404()
    a.is_published = not a.is_published
    db.session.commit()
    flash("تم النشر" if a.is_published else "تم الإخفاء", "success")
    return redirect(url_for("superadmin.help_edit", article_id=a.id))


@bp.route("/help/<int:article_id>/delete", methods=["POST"])
@login_required
@superadmin_required
def help_delete(article_id):
    from app.models import HelpArticle
    a = db.session.get(HelpArticle, article_id) or _404()
    db.session.delete(a); db.session.commit()
    flash("تم حذف المقال", "success")
    return redirect(url_for("superadmin.help_index"))


@bp.route("/help/<int:article_id>/examples", methods=["POST"])
@login_required
@superadmin_required
def help_add_example(article_id):
    from app.models import HelpArticle, HelpArticleExample
    a = db.session.get(HelpArticle, article_id) or _404()
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not title:
        flash("عنوان المثال مطلوب", "error")
    else:
        order = int(request.form.get("display_order") or
                     (len(a.examples) + 1))
        db.session.add(HelpArticleExample(
            article_id=a.id, title=title, body=body,
            display_order=order))
        db.session.commit()
        flash("تمت إضافة المثال", "success")
    return redirect(url_for("superadmin.help_edit", article_id=a.id))


@bp.route("/help/examples/<int:example_id>/delete", methods=["POST"])
@login_required
@superadmin_required
def help_delete_example(example_id):
    from app.models import HelpArticleExample
    ex = db.session.get(HelpArticleExample, example_id) or _404()
    aid = ex.article_id
    db.session.delete(ex); db.session.commit()
    flash("حُذف المثال", "success")
    return redirect(url_for("superadmin.help_edit", article_id=aid))


@bp.route("/help/<int:article_id>/media", methods=["POST"])
@login_required
@superadmin_required
def help_add_media(article_id):
    from app.models import (
        HelpArticle, HelpArticleMedia, MEDIA_IMAGE, MEDIA_LINK,
    )
    from app.services.help_media import (
        save_image, extract_video, HelpMediaError,
    )
    a = db.session.get(HelpArticle, article_id) or _404()
    kind = (request.form.get("kind") or "").strip().upper()
    caption = (request.form.get("caption") or "").strip() or None
    order = int(request.form.get("display_order") or
                 (len(a.media) + 1))
    try:
        if kind == "IMAGE":
            f = request.files.get("file")
            key = save_image(f)
            db.session.add(HelpArticleMedia(
                article_id=a.id, type=MEDIA_IMAGE,
                file_path=key, caption=caption,
                display_order=order))
        elif kind == "VIDEO":
            url = (request.form.get("url") or "").strip()
            parsed = extract_video(url)
            if not parsed:
                raise HelpMediaError(
                    "رابط الفيديو غير صحيح (يوتيوب أو فيميو فقط)")
            vtype, video_id = parsed
            db.session.add(HelpArticleMedia(
                article_id=a.id, type=vtype,
                url=video_id, caption=caption,
                display_order=order))
        elif kind == "LINK":
            url = (request.form.get("url") or "").strip()
            if not url:
                raise HelpMediaError("الرابط مطلوب")
            db.session.add(HelpArticleMedia(
                article_id=a.id, type=MEDIA_LINK,
                url=url, caption=caption,
                display_order=order))
        else:
            raise HelpMediaError("نوع الوسيلة غير معروف")
        db.session.commit()
        flash("تمت إضافة الوسيلة", "success")
    except HelpMediaError as e:
        flash(str(e), "error")
    return redirect(url_for("superadmin.help_edit", article_id=a.id))


@bp.route("/help/media/<int:media_id>/delete", methods=["POST"])
@login_required
@superadmin_required
def help_delete_media(media_id):
    from app.models import HelpArticleMedia
    m = db.session.get(HelpArticleMedia, media_id) or _404()
    aid = m.article_id
    db.session.delete(m); db.session.commit()
    flash("حُذفت الوسيلة", "success")
    return redirect(url_for("superadmin.help_edit", article_id=aid))


@bp.route("/help/<int:article_id>/preview")
@login_required
@superadmin_required
def help_preview(article_id):
    """Render the article using the public template but with an
    unpublished-preview banner. Lets the author see the final shape
    before flipping is_published."""
    from app.models import HelpArticle
    a = db.session.get(HelpArticle, article_id) or _404()
    return render_template("help/article.html", article=a,
                             preview=True)


def _split_lines(raw):
    """Textarea → list of non-empty stripped strings, one per line."""
    if not raw:
        return []
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


# ─── MARSOUD-SAAS-BILLING-01 (Batch 5 Ticket 7, 2026-07-29) ───
@bp.route("/saas")
@login_required
@superadmin_required
def saas_index():
    """Cross-tenant SaaS billing dashboard. Shows each company's
    current plan, frequency, subscription state, latest outstanding
    invoice, and a mark-paid button.

    MARSOUD-TKT-SAAS-INDEX-COMPANY-FILTER (2026-08-31) — supports an
    optional ?company_id=<id> query param. When set, the list narrows
    to that single tenant (used from /admin/companies/<id> "فواتير
    SaaS مفتوحة → راجع" so clicking the link on Company A no longer
    dumps every tenant's outstanding invoices on the reader).
    """
    from app.models import Company, Plan, Invoice, InvoiceStatus
    from app.services import saas_billing as _sb

    filter_company_id = request.args.get("company_id", type=int)
    filter_company = None
    if filter_company_id:
        filter_company = db.session.get(Company, filter_company_id)
        # Silently ignore an invalid id — treat as "no filter" rather
        # than 404, since this is a nav parameter, not a resource.
        if not filter_company or filter_company.deleted_at is not None:
            filter_company = None
            filter_company_id = None

    outstanding = _sb.outstanding_saas_invoices()
    # Map outstanding invoices → tenant company (via saas_customer_id).
    inv_by_tenant = {}
    for inv in outstanding:
        t = Company.query.filter_by(
            saas_customer_id=inv.customer_id).first()
        if t:
            inv_by_tenant.setdefault(t.id, []).append(inv)

    # Show every non-deleted company with an intended_plan_id — or
    # just the one company when the filter is active.
    q = (Company.query
         .filter(Company.deleted_at.is_(None),
                 Company.intended_plan_id.isnot(None)))
    if filter_company_id:
        q = q.filter(Company.id == filter_company_id)
    companies = q.order_by(Company.name).all()

    plans_lookup = {p.id: p for p in Plan.query.all()}
    return render_template(
        "admin/saas_index.html",
        companies=companies,
        plans_lookup=plans_lookup,
        outstanding_by_tenant=inv_by_tenant,
        filter_company=filter_company,
    )


@bp.route("/saas/invoices/<int:invoice_id>/mark-paid",
          methods=["POST"])
@login_required
@superadmin_required
def saas_mark_paid(invoice_id):
    from app.models import Invoice
    from app.services import saas_billing as _sb
    inv = db.session.get(Invoice, invoice_id) or _404()
    try:
        tenant = _sb.mark_saas_invoice_paid(inv, current_user.id)
        log_platform_action(
            "saas.mark_paid",
            f"invoice #{inv.number} marked paid for {tenant.name}")
        flash(f"تم تسجيل الدفعة + تجديد اشتراك {tenant.name}",
              "success")
    except _sb.SaasBillingError as e:
        flash(str(e), "error")
    return redirect(url_for("superadmin.saas_index"))


# ─── MARSOUD-TKT-ADMIN-VOID-SAAS-INVOICE (2026-08-31) ─────────────
# Shortcut so Abdelhamid can void a SaaS invoice straight from
# /admin/saas without switching-into company 8 (Manasety) and
# opening its invoice list. Same behaviour as the tenant-side
# invoices.delete route — they both call
# services.invoicing.void_invoice so the audit trail + refund
# journals are identical either way.
#
# Reason is REQUIRED here (blank submission returns to the list
# with a validation flash) — the tenant-side route defaults to
# "حذف الفاتورة" when empty, which is fine there because the
# user is standing inside the tenant company; for the admin
# shortcut, a real reason is important since the target company
# owner never had the chance to ask for it.
@bp.route("/saas/invoices/<int:invoice_id>/void", methods=["POST"])
@login_required
@superadmin_required
def saas_void_invoice(invoice_id):
    from app.models import Invoice
    from app.services.invoicing import void_invoice
    from app.services.ledger import LedgerError

    inv = db.session.get(Invoice, invoice_id) or _404()
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("سبب الحذف مطلوب — اكتبه في popup قبل التأكيد",
              "error")
        return redirect(url_for("superadmin.saas_index"))

    invoice_number = inv.number
    invoice_company_id = inv.company_id
    try:
        outcome = void_invoice(inv, reason, current_user.id)
    except RuntimeError as e:
        flash(str(e), "warning")
        return redirect(url_for("superadmin.saas_index"))
    except (LedgerError, KeyError) as e:
        flash(f"فشل حذف الفاتورة: {e}", "error")
        return redirect(url_for("superadmin.saas_index"))

    log_platform_action(
        "saas.invoice_voided",
        target_company_id=invoice_company_id,
        actor_id=current_user.id,
        details=(f"invoice #{invoice_number} voided from admin — "
                  f"reason: {reason[:80]}"),
    )
    if outcome == "deleted":
        flash(f"تم حذف الفاتورة {invoice_number}", "success")
    else:
        flash(
            f"تم إلغاء الفاتورة {invoice_number} — تظهر في المرتجعات بنفس السبب",
            "success")
    return redirect(url_for("superadmin.saas_index"))


@bp.route("/saas/companies/<int:company_id>/price-lock",
          methods=["POST"])
@login_required
@superadmin_required
def saas_price_lock(company_id):
    from app.models import Company
    c = db.session.get(Company, company_id) or _404()
    raw = (request.form.get("price_lock") or "").strip()
    if not raw:
        c.price_lock = None
        flash(f"تم إلغاء قفل السعر لـ {c.name}", "success")
    else:
        try:
            from decimal import Decimal
            c.price_lock = Decimal(raw)
            flash(f"تم قفل السعر لـ {c.name} على {c.price_lock}",
                  "success")
        except (ValueError, ArithmeticError):
            flash("قيمة السعر غير صحيحة", "error")
            return redirect(url_for("superadmin.saas_index"))
    db.session.commit()
    return redirect(url_for("superadmin.saas_index"))


@bp.route("/ai-usage")
@login_required
@superadmin_required
def ai_usage():
    from app.services.superadmin import ai_usage_overview
    rows = ai_usage_overview()
    return render_template("admin/ai_usage.html", rows=rows)


# ─── MARSOUD-AGENT-DEEPSEEK-02 (2026-08-06) ────────────────────────────
_ACCOUNTANT_PROVIDERS = ("anthropic", "deepseek")


@bp.route("/ai-settings", methods=["GET", "POST"])
@login_required
@superadmin_required
def ai_settings():
    """Runtime knobs for the accountant agent — provider and model.

    Two PlatformSetting keys read on every accountant turn (see
    app/agent/base.py::get_accountant_provider_and_model). No app
    restart needed after a save. Insights persona is a separate
    ticket — this screen only touches the accountant.
    """
    from app.services.subscription import _set_setting_raw
    from app.agent.base import (
        get_accountant_provider_and_model,
        _ACCOUNTANT_DEFAULT_MODEL_BY_PROVIDER,
    )

    if request.method == "POST":
        provider = (request.form.get("accountant_provider")
                    or "").strip().lower()
        if provider not in _ACCOUNTANT_PROVIDERS:
            flash("مزود غير معروف — اختر anthropic أو deepseek",
                  "error")
            return redirect(url_for("superadmin.ai_settings"))

        model = (request.form.get("accountant_model") or "").strip()
        # An empty model falls back to the per-provider default on
        # read — safer than saving an empty string that would then
        # be used verbatim by the provider SDK.
        _set_setting_raw("accountant_provider", provider)
        _set_setting_raw("accountant_model", model or "")

        # MARSOUD-AGENT-SAFETY-03 (2026-08-06) — two new knobs land on
        # the same form: the confirmation toggle and the daily write
        # cap. Silently skipped when the field is missing so an older
        # POST (pre-T3) doesn't blow away a saved value.
        conf_raw = request.form.get("agent_require_confirmation")
        if conf_raw is not None:
            _set_setting_raw("agent_require_confirmation",
                              "true" if conf_raw == "on" else "false")
        cap_raw = (request.form.get("agent_daily_write_cap")
                    or "").strip()
        if cap_raw.isdigit() and 0 <= int(cap_raw) <= 10_000:
            _set_setting_raw("agent_daily_write_cap", str(int(cap_raw)))

        # MARSOUD-AGENT-MEMORY-05 (2026-08-06) — retention days for
        # agent conversations. 0 means never expire (deliberate
        # non-destructive default when the field is fat-fingered).
        ret_raw = (request.form.get(
            "agent_conversation_retention_days") or "").strip()
        if ret_raw.isdigit() and 0 <= int(ret_raw) <= 3650:
            _set_setting_raw(
                "agent_conversation_retention_days",
                str(int(ret_raw)))

        db.session.commit()
        log_platform_action(
            "ai_settings_update",
            actor_id=current_user.id,
            details=(f"accountant_provider={provider} "
                     f"model={model or '(default)'} "
                     f"require_confirmation={conf_raw or 'unchanged'} "
                     f"daily_cap={cap_raw or 'unchanged'}"))
        flash("تم حفظ إعدادات الذكاء الاصطناعي", "success")
        return redirect(url_for("superadmin.ai_settings"))

    provider, model = get_accountant_provider_and_model()
    from app.services.agent_safety import (
        require_confirmation_enabled, daily_write_cap,
    )
    from app.services.agent_conversations import retention_days
    return render_template(
        "admin/ai_settings.html",
        current_provider=provider,
        current_model=model,
        providers=_ACCOUNTANT_PROVIDERS,
        provider_defaults=_ACCOUNTANT_DEFAULT_MODEL_BY_PROVIDER,
        require_confirmation=require_confirmation_enabled(),
        daily_write_cap=daily_write_cap(),
        conversation_retention_days=retention_days(),
    )


# ── MARSOUD-APPROVAL-GATED-SUPERADMIN (2026-08-12) ───────── #
# Two routes: the approval inbox + the bulk decide endpoint.
# Both are unwrapped POST/GET and NOT registered in
# services/superadmin_approval.py::DESTRUCTIVE_ENDPOINTS —
# so the gate's fail-safe would 403 a restricted user on
# POST, and the explicit `if requires_approval → abort(403)`
# in the view body enforces it on GET too (belt + braces).
@bp.route("/pending-actions")
@login_required
@superadmin_required
def pending_actions():
    """Primary superadmin's approval inbox. Lists every
    pending row across all restricted actors, newest first."""
    from app.models import PendingSuperadminAction
    if getattr(current_user, "requires_approval", False):
        # A restricted user cannot approve their own actions.
        abort(403)
    rows = (PendingSuperadminAction.query
            .filter_by(status="pending")
            .order_by(PendingSuperadminAction.created_at.desc())
            .all())
    from app.services.superadmin_approval import ENDPOINT_LABELS_AR
    return render_template(
        "admin/pending_actions.html",
        rows=rows,
        endpoint_labels=ENDPOINT_LABELS_AR,
    )


@bp.route("/pending-actions/decide", methods=["POST"])
@login_required
@superadmin_required
def pending_actions_decide():
    """Bulk approve or reject the ticked rows. Reads
    action_id[] + decision + note from the form."""
    from app.services.superadmin_approval import (
        execute_pending, reject_pending, ApprovalError,
    )
    if getattr(current_user, "requires_approval", False):
        abort(403)
    ids = [int(x) for x in request.form.getlist("action_id") if x]
    decision = (request.form.get("decision") or "").strip()
    note = (request.form.get("note") or "").strip() or None
    if decision not in ("approve", "reject") or not ids:
        flash("اختر إجراء واحد على الأقل وحدد موافقة أو رفض",
              "error")
        return redirect(url_for("superadmin.pending_actions"))
    ok = err = 0
    errors = []
    for aid in ids:
        try:
            if decision == "approve":
                execute_pending(aid, approver_id=current_user.id,
                                 note=note)
            else:
                reject_pending(aid, approver_id=current_user.id,
                                note=note)
            ok += 1
        except ApprovalError as e:
            err += 1
            errors.append(f"#{aid}: {e}")
        except Exception as e:  # noqa: BLE001 — surface, don't swallow
            db.session.rollback()
            err += 1
            errors.append(f"#{aid}: {type(e).__name__}: {e}")
    if ok:
        label = "الموافقة على" if decision == "approve" else "رفض"
        flash(f"تمت {label} {ok} إجراء بنجاح", "success")
    if err:
        flash(f"فشل {err} إجراء — " + " · ".join(errors), "error")
    return redirect(url_for("superadmin.pending_actions"))
