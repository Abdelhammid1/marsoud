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

        # MARSOUD-LOCKOUT-RESET (Abdelhamid 2026-07-22) — refuse any
        # attempt during the lock window, even a correct password.
        # Prevents the timing signal "wrong pw is faster than correct
        # pw" from being useful during brute-force.
        if user and user.locked_until and user.locked_until > datetime.utcnow():
            remaining = int(
                (user.locked_until - datetime.utcnow()).total_seconds() // 60) + 1
            flash(
                f"الحساب مقفل مؤقتاً بسبب محاولات دخول متكررة. "
                f"جرّب بعد {remaining} دقيقة.",
                "error",
            )
            return render_template("auth/login.html")

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
            # MARSOUD-LOCKOUT-RESET — successful login resets the
            # failed-attempts counter.
            user.failed_login_attempts = 0
            user.locked_until = None
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

        # MARSOUD-LOCKOUT-RESET — wrong password: bump counter, lock
        # at the threshold. Only count when we actually found a user
        # to avoid enumerating valid emails via lockout behaviour.
        if user:
            user.failed_login_attempts = (
                (user.failed_login_attempts or 0) + 1)
            if user.failed_login_attempts >= 5:
                from datetime import timedelta as _td
                user.locked_until = datetime.utcnow() + _td(minutes=15)
            db.session.commit()
        flash("بيانات الدخول غير صحيحة", "error")
    return render_template("auth/login.html")


# ─── MARSOUD-LOCKOUT-RESET (Abdelhamid 2026-07-22) — forgot pw ───
@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Ask for an email, mail a reset link. Always shows the same
    'sent' message regardless of whether the email exists — prevents
    account-enumeration via error text."""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = User.query.filter_by(email=email).first() if email else None
        if user:
            try:
                _send_password_reset_email(user)
            except Exception:
                from flask import current_app
                current_app.logger.exception("password reset email failed")
        flash(
            "لو الإيميل مسجّل عندنا، هتلاقي رسالة فيها رابط إعادة "
            "التعيين خلال دقيقة.",
            "success",
        )
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html")


def _send_password_reset_email(user):
    from app.services.permissions import generate_password_reset_token
    from app.services.email import send_email
    token = generate_password_reset_token(user)
    subdomain = None
    if user.companies:
        subdomain = user.companies[0].subdomain
    if subdomain:
        reset_url = (f"https://{subdomain}.marsoud.com"
                     + url_for("auth.reset_password", token=token))
    else:
        reset_url = url_for("auth.reset_password", token=token,
                            _external=True)
    subject = "إعادة تعيين كلمة السر — مرصود"
    html = render_template("auth/reset_link_email.html",
                            user=user, reset_url=reset_url)
    send_email(user.email, subject, html)


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    from app.services.permissions import parse_password_reset_token
    from app.services.password_policy import validate_password
    payload = parse_password_reset_token(token)
    if not payload:
        flash("رابط إعادة التعيين غير صحيح أو منتهي الصلاحية.", "error")
        return redirect(url_for("auth.forgot_password"))
    user = db.session.get(User, int(payload.get("user_id") or 0))
    if not user:
        flash("رابط غير صحيح.", "error")
        return redirect(url_for("auth.forgot_password"))
    # Anti-replay: token was signed against a snapshot of the pw hash;
    # if the pw has changed since, the snapshot won't match anymore.
    if payload.get("h") != (user.password_hash or "")[-12:]:
        flash("الرابط تم استخدامه بالفعل — اطلب رابط جديد.", "error")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        new = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if new != confirm:
            flash("كلمة السر وتأكيدها غير متطابقين.", "error")
            return render_template("auth/reset_password.html", token=token)
        ok, reason = validate_password(new)
        if not ok:
            flash(reason, "error")
            return render_template("auth/reset_password.html", token=token)
        user.set_password(new)
        # Reset lockout state as a courtesy — if the user forgot
        # their password and locked themselves out, the reset should
        # unlock them.
        user.failed_login_attempts = 0
        user.locked_until = None
        db.session.commit()
        flash("تم تغيير كلمة السر بنجاح — سجل دخولك بها.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", token=token)


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
        # MARSOUD-EMAIL-VERIFY (Abdelhamid 2026-07-22) — self-service
        # signups start as PENDING_VERIFICATION until they click the
        # link in the welcome email. The middleware in app/__init__.py
        # redirects them to /auth/verify-pending on every dashboard
        # request until they do.
        from app.models import UserStatus
        user.status = UserStatus.PENDING_VERIFICATION.value
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
        # MARSOUD-EMAIL-VERIFY — send verification email. Failure is
        # non-fatal (SMTP might be down in dev) — the user can hit
        # /auth/verify/resend from the pending page.
        try:
            _send_verify_email(user)
        except Exception:
            from flask import current_app
            current_app.logger.exception("verify email send failed")
        login_user(user)
        session["active_company_id"] = company.id
        flash(
            "تم إنشاء الحساب — راجع بريدك الإلكتروني لتفعيل الحساب.",
            "success",
        )
        # Land on the "check your email" page inside the new tenant
        # subdomain so the middleware doesn't need to redirect again.
        return redirect(
            f"https://{subdomain}.marsoud.com" + url_for("auth.verify_pending"))
    return render_template("auth/register.html")


# ─── MARSOUD-EMAIL-VERIFY (Abdelhamid 2026-07-22) — verify flow ───
def _send_verify_email(user):
    """Send the welcome + verify link email. Called from /register and
    from the resend endpoint. Uses the itsdangerous-based
    generate_verify_email_token so the link expires after 7 days."""
    from app.services.permissions import generate_verify_email_token
    from app.services.email import send_email
    token = generate_verify_email_token(user.id)
    # Prefer the tenant's subdomain if the user has a company attached.
    subdomain = None
    if user.companies:
        subdomain = user.companies[0].subdomain
    if subdomain:
        verify_url = (f"https://{subdomain}.marsoud.com"
                      + url_for("auth.verify_email", token=token))
    else:
        verify_url = url_for("auth.verify_email", token=token,
                             _external=True)
    subject = "تفعيل حسابك في مرصود"
    html = render_template("auth/verify_link_email.html",
                            user=user, verify_url=verify_url)
    send_email(user.email, subject, html)


@bp.route("/verify/<token>")
def verify_email(token):
    """Consume a verify-email token. Marks the user ACTIVE + records
    email_verified_at. Idempotent — a repeat click just says
    'already verified' instead of erroring."""
    from app.services.permissions import parse_verify_email_token
    from app.models import UserStatus
    payload = parse_verify_email_token(token)
    if not payload:
        flash("رابط التفعيل غير صحيح أو منتهي الصلاحية.", "error")
        return redirect(url_for("auth.login"))
    uid = int(payload.get("user_id") or 0)
    user = db.session.get(User, uid)
    if not user:
        flash("رابط التفعيل غير صحيح.", "error")
        return redirect(url_for("auth.login"))
    if user.email_verified_at:
        flash("الحساب مفعّل بالفعل — تفضّل بتسجيل الدخول.", "success")
        return redirect(url_for("auth.login"))
    user.email_verified_at = datetime.utcnow()
    user.status = UserStatus.ACTIVE.value
    user.is_active = True
    db.session.commit()
    flash("تم تفعيل حسابك بنجاح 🎉 يمكنك الآن تسجيل الدخول.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/verify-pending")
@login_required
def verify_pending():
    """Landing page after signup + destination the middleware sends
    unverified users to. Shows a friendly 'check your email' message
    with a resend button."""
    return render_template("auth/verify_pending.html")


@bp.route("/verify/resend", methods=["POST"])
@login_required
def verify_resend():
    """Fire another verification email — cheap idempotent operation."""
    try:
        _send_verify_email(current_user)
        flash("تم إعادة إرسال رسالة التفعيل — راجع بريدك.", "success")
    except Exception:
        from flask import current_app
        current_app.logger.exception("verify email resend failed")
        flash("تعذّر إرسال البريد الآن — حاول لاحقاً.", "error")
    return redirect(url_for("auth.verify_pending"))


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
