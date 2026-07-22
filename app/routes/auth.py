from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_user, logout_user, login_required, current_user
from app import db
from app.models import User, Company
from app.services.seed_coa import seed_default_coa
from app.models.payroll import Employee
from app.services.superadmin import log_platform_action, end_impersonation

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            # Inactive accounts (pending activation or disabled) can't sign in.
            if not user.is_active:
                flash("حسابك غير مفعّل أو موقوف. تواصل مع مالك الشركة.", "warning")
                return render_template("auth/login.html")
            active_companies = [c for c in user.companies
                                if (c.status or "ACTIVE") != "SUSPENDED"]
            if user.companies and not active_companies and not user.is_superadmin:
                flash("كل شركاتك موقوفة. تواصل مع مالك المنصة.", "error")
                return render_template("auth/login.html")
            login_user(user, remember=True)
            user.last_login_at = datetime.utcnow()
            db.session.commit()
            log_platform_action("user_login", actor_id=user.id,
                                target_user_id=user.id)
            active_cid = active_companies[0].id if active_companies else None
            if active_cid:
                session["active_company_id"] = active_cid
            # MARSOUD-ACTLOG-01 — start a session row + log LOGIN action.
            try:
                from app.services.activity import start_session, log_action
                start_session(user, company_id=active_cid)
                log_action(
                    action_type="LOGIN", entity_type="user",
                    entity_id=user.id,
                    entity_label=user.full_name or user.email,
                )
            except Exception:
                pass
            if user.is_superadmin:
                return redirect(url_for("superadmin.dashboard"))
            return redirect(url_for("dashboard.index"))
        flash("بيانات الدخول غير صحيحة", "error")
    return render_template("auth/login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        full_name = request.form.get("full_name", "").strip()
        company_name = request.form.get("company_name", "").strip()
        subdomain = request.form.get("subdomain", "").strip().lower()
        password = request.form.get("password", "")
        base_currency = request.form.get("base_currency", "SAR")

        if not email or not password or not full_name or not company_name or not subdomain:
            flash("جميع الحقول مطلوبة", "error")
            return render_template("auth/register.html")

        # MARSOUD-PASSWORD-POLICY — one central validator so signup,
        # invitation accept, super-admin reset, and HR self-service all
        # agree on the same rules.
        from app.services.password_policy import validate_password
        ok, reason = validate_password(password)
        if not ok:
            flash(reason, "error")
            return render_template("auth/register.html")

        from app.models.company import is_valid_subdomain
        if not is_valid_subdomain(subdomain):
            flash(
                "عنوان الشركة غير صالح — من 3 إلى 63 حرفًا، أحرف إنجليزية صغيرة وأرقام وشرطات فقط",
                "error",
            )
            return render_template("auth/register.html")

        if Company.query.filter_by(subdomain=subdomain).first():
            flash("عنوان الشركة هذا محجوز بالفعل، جرّب عنوانًا آخر", "error")
            return render_template("auth/register.html")

        if User.query.filter_by(email=email).first():
            flash("الإيميل مستخدم بالفعل", "error")
            return render_template("auth/register.html")

        user = User(email=email, full_name=full_name)
        user.set_password(password)
        company = Company(name=company_name, base_currency=base_currency, subdomain=subdomain)
        # Bug fix (abdelhamid) — every new company gets the one-month
        # default subscription window + the enterprise plan, instead of
        # being created without any subscription state.
        from app.services.subscription import activate_default_subscription
        activate_default_subscription(company)
        user.companies.append(company)
        db.session.add(user)
        db.session.flush()
        # MARSOUD-MC-EMPLOYEE — link on Employee.user_id (per-company).
        owner_emp = Employee(
            company_id=company.id,
            name=user.full_name,
            email=user.email,
            user_id=user.id,
        )
        db.session.add(owner_emp)
        db.session.commit()
        seed_default_coa(company.id)
        login_user(user)
        session["active_company_id"] = company.id
        flash("تم إنشاء الحساب وشجرة الحسابات الافتراضية", "success")
        # MARSOUD-SAAS-SUBDOMAIN — send them to their new tenant subdomain.
        # SESSION_COOKIE_DOMAIN=.marsoud.com keeps them logged in across it.
        return redirect(f"https://{subdomain}.marsoud.com" + url_for("dashboard.index"))
    return render_template("auth/register.html")


@bp.route("/logout")
@login_required
def logout():
    end_impersonation()
    # MARSOUD-ACTLOG-01 — close the session row + log LOGOUT BEFORE
    # logout_user() so we still have current_user for the action row.
    try:
        from app.services.activity import (
            end_session_by_token, log_action, SESSION_KEY,
        )
        log_action(action_type="LOGOUT", entity_type="user",
                   entity_id=current_user.id,
                   entity_label=current_user.full_name or current_user.email)
        end_session_by_token(session.get(SESSION_KEY))
        session.pop(SESSION_KEY, None)
    except Exception:
        pass
    logout_user()
    session.pop("active_company_id", None)
    return redirect(url_for("auth.login"))


# ─── MARSOUD-ACTLOG-01: heartbeat ───────────────────────────────────────
@bp.route("/heartbeat", methods=["POST"])
@login_required
def heartbeat():
    """Frontend pings every 5 min from base.html so we can tell the
    difference between 'user closed the browser' and 'user is still
    looking at the page'. Does NOT write to user_activity_log — too
    chatty; it only bumps last_seen_at on the open UserSession row."""
    from app.services.activity import heartbeat as _hb, SESSION_KEY
    sid = session.get(SESSION_KEY)
    if sid:
        _hb(sid)
    return ("", 204)


@bp.route("/switch-company/<int:company_id>")
@login_required
def switch_company(company_id):
    company = db.session.get(Company, company_id)
    if company and company in current_user.companies:
        session["active_company_id"] = company.id
        flash(f"تم التبديل إلى {company.name}", "success")
    return redirect(request.referrer or url_for("dashboard.index"))
