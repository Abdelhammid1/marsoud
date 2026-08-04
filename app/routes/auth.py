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
        # MARSOUD-BOT-PROTECTION-01 (Abdelhamid 2026-07-24) — three
        # cheap gates BEFORE any DB read/write. Order matters: the
        # honeypot check first (soft-fail, silent), then rate limit
        # (429), then spam-domain check (403).
        from app.services.bot_guard import (
            honeypot_tripped, register_rate_ok, is_spam_email, client_ip,
            verify_turnstile, is_turnstile_enabled,
        )
        if honeypot_tripped(request.form):
            # DO NOT return an error — that tells the bot the trap
            # exists. Answer with a plain "OK" page and quietly
            # discard the request. To the bot, everything worked.
            return render_template("auth/register_success_decoy.html"), 200
        ip = client_ip(request)
        if not register_rate_ok(ip):
            flash("تم تجاوز الحد المسموح من محاولات التسجيل. حاول لاحقاً.",
                  "error")
            return render_template("auth/register.html"), 429
        # Cloudflare Turnstile — no-op when the keys aren't set
        # (dev/local). In prod both keys are populated so bots that
        # skip the widget or reuse tokens are rejected here.
        ts_token = request.form.get("cf-turnstile-response", "")
        if not verify_turnstile(ts_token, remote_ip=ip):
            flash("فشل التحقق من كونك بشراً. حاول تحديث الصفحة.", "error")
            return render_template("auth/register.html"), 403

        email = request.form.get("email", "").strip().lower()
        full_name = request.form.get("full_name", "").strip()
        company_name = request.form.get("company_name", "").strip()
        subdomain = request.form.get("subdomain", "").strip().lower()
        password = request.form.get("password", "")
        # MARSOUD-MULTI-CURRENCY-PRICING (Abdelhamid 2026-07-22) —
        # default currency flipped SAR → EGP.
        base_currency = request.form.get("base_currency", "EGP")
        # MARSOUD-REGISTRATION-PHONES-01 (Batch 6 Ticket 4,
        # 2026-07-29) — optional phone fields, capped at 50 chars
        # and stripped. Both optional (blank stored as NULL) so
        # signup stays backward compatible with any old
        # bookmarked form.
        company_phone = (request.form.get("company_phone")
                           or "").strip()[:50] or None
        owner_phone = (request.form.get("owner_phone")
                         or "").strip()[:50] or None

        if not email or not password or not full_name or not company_name or not subdomain:
            flash("جميع الحقول مطلوبة", "error")
            return render_template("auth/register.html")

        if is_spam_email(email):
            flash("عنوان بريد إلكتروني غير مقبول. استخدم بريدك الحقيقي.",
                  "error")
            return render_template("auth/register.html"), 403

        # MARSOUD-PASSWORD-POLICY — one central validator so signup,
        # invitation accept, super-admin reset, and HR self-service all
        # agree on the same rules.
        from app.services.password_policy import validate_password
        ok, reason = validate_password(password)
        if not ok:
            flash(reason, "error")
            return render_template("auth/register.html")

        # MARSOUD-TERMS-CONSENT (Abdelhamid 2026-07-22) — mandatory
        # checkbox. HTML `required` catches most, this is the server-
        # side safety net.
        if request.form.get("agree_terms") != "on":
            flash(
                "الموافقة على الشروط والأحكام وسياسة الخصوصية مطلوبة.",
                "error",
            )
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

        user = User(email=email, full_name=full_name,
                    phone=owner_phone)
        user.set_password(password)
        # MARSOUD-EMAIL-VERIFY (Abdelhamid 2026-07-22) — self-service
        # signups start as PENDING_VERIFICATION until they click the
        # link in the welcome email. The middleware in app/__init__.py
        # redirects them to /auth/verify-pending on every dashboard
        # request until they do.
        from app.models import UserStatus
        user.status = UserStatus.PENDING_VERIFICATION.value
        # MARSOUD-TERMS-CONSENT — record the audit trail. Version comes
        # from platform_settings so a later version-bump can re-prompt.
        from app.services.legal import get_terms_version, record_consent
        user.terms_accepted_at = datetime.utcnow()
        user.terms_version = get_terms_version()
        company = Company(name=company_name, base_currency=base_currency,
                          subdomain=subdomain, phone=company_phone,
                          # MARSOUD-CURRENCY-TAX-DEFAULTS (Batch 8
                          # Ticket 4b, 2026-07-30) — new companies
                          # start at 0% VAT so tenants outside VAT-
                          # jurisdictions don't have to manually
                          # clear it. Existing companies keep their
                          # previously-saved rate (column default
                          # is still 15 for back-compat).
                          vat_rate=0)
        # MARSOUD-CHOOSE-PLAN (Abdelhamid 2026-07-22) — was auto-
        # assigning the Enterprise plan at signup. Now: only stamp the
        # trial window; leave plan_id + intended_plan_id NULL so the
        # user lands on /choose-plan after email verify. plan_gating
        # treats any company inside its trial window as full-access
        # regardless of intended_plan_id.
        from app.services.subscription import activate_default_subscription
        activate_default_subscription(company, plan_code=None)
        user.companies.append(company)
        db.session.add(user)
        db.session.flush()
        # MARSOUD-CONSENT-AUDIT-LOG — immutable per-event history.
        # Deliberately AFTER the flush so user.id is populated.
        record_consent(user, source="signup",
                        company_id=company.id, request=request)
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
        # MARSOUD-ROLE-SYNC — `user.companies.append(company)` above goes
        # through the ORM secondary table, so `role` falls back to the
        # column default and `role_id` stays NULL until the next app
        # boot runs the backfill. Seed the roles now and write both
        # columns so the owner has a usable role from the first request.
        try:
            from app.services.roles_seed import ensure_roles_ready_for_company
            from app.services.roles import set_membership_role
            ensure_roles_ready_for_company(company.id)
            set_membership_role(user.id, company.id, "owner")
        except Exception:
            from flask import current_app
            current_app.logger.exception("seed system roles failed")
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


# ─── MARSOUD-CHOOSE-PLAN (Abdelhamid 2026-07-22) — post-signup ───
@bp.route("/choose-plan", methods=["GET", "POST"])
@login_required
def choose_plan():
    """Post-signup plan selection. Owner picks Starter/Growth/Pro (or
    whatever active plans exist). During the trial the choice is
    only recorded — enforcement kicks in after subscription_expires_at.
    """
    from app.models import Plan, Company
    from flask import g as _g
    company = _g.get("active_company")
    if not company:
        # No active tenant → nothing to choose against. Just land them.
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        pid = request.form.get("plan_id", type=int)
        plan = db.session.get(Plan, pid) if pid else None
        if not plan or not plan.is_active:
            flash("الباقة المختارة غير صحيحة.", "error")
            return redirect(url_for("auth.choose_plan"))
        company.intended_plan_id = plan.id
        # MARSOUD-SAAS-BILLING-01 (Batch 5 Ticket 7, 2026-07-29) —
        # capture the frequency the owner picked so the SaaS
        # billing loop knows how to time the next invoice.
        freq = (request.form.get("frequency") or "MONTHLY").strip().upper()
        if freq not in ("MONTHLY", "YEARLY"):
            freq = "MONTHLY"
        company.subscription_frequency = freq

        # MARSOUD-DISCOUNT-COUPONS wiring (Abdelhamid 2026-07-29) —
        # optional coupon on /choose-plan. Validate immediately so a
        # bad code doesn't silently slip through. On success, stash
        # the coupon_id on the company; the SaaS billing service
        # applies the discount to the first invoice + calls
        # coupons.redeem() ONLY after payment is confirmed (per the
        # ticket: "نداء redeem() بعد نجاح العملية فعليًا").
        coupon_code = (request.form.get("coupon_code") or "").strip()
        if coupon_code:
            from app.services import coupons as _cp
            try:
                base_price = plan.price_monthly or 0
                coupon, _amt = _cp.validate(
                    coupon_code, company, plan.id, base_price)
                company.applied_coupon_id = coupon.id
                flash(
                    f"سجّلنا اختيارك للباقة {plan.name_ar or plan.name} "
                    f"+ كود الخصم '{coupon_code}'. الخصم يُطبّق على "
                    f"أول فاتورة بعد نهاية الفترة التجريبية.",
                    "success",
                )
            except _cp.CouponError as e:
                # Don't lose the plan choice on a bad coupon — save
                # it and warn about the coupon only.
                db.session.commit()
                flash(f"الباقة اتحفظت — لكن كود الخصم: {e}", "warning")
                return redirect(url_for("auth.choose_plan"))
        else:
            flash(
                f"سجّلنا اختيارك للباقة: {plan.name_ar or plan.name}. "
                f"تمتّع بالفترة التجريبية.",
                "success",
            )
        db.session.commit()
        # MARSOUD-SAAS-BILLING-01 (Batch 5 Ticket 7, 2026-07-29) —
        # kick off the first Manasty-side subscription invoice. We
        # do this AFTER the commit so a failure creating the invoice
        # doesn't roll back the plan pick (owner keeps their choice
        # + we can retry the invoice later from admin UI).
        try:
            from flask import current_app as _ca
            from app.services import saas_billing as _sb
            _sb.create_first_invoice(company)
            db.session.commit()
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            from flask import current_app as _ca
            _ca.logger.warning(
                "SaaS first-invoice creation failed for company "
                "%s: %s", company.id, e)
        return redirect(url_for("dashboard.index"))

    plans = Plan.query.filter_by(is_active=True).order_by(
        Plan.price_monthly.asc().nullslast()).all()
    return render_template(
        "auth/choose_plan.html", plans=plans, company=company)


# ─── MARSOUD-TERMS-CONSENT (Abdelhamid 2026-07-22) — re-accept ───
@bp.route("/re-accept-terms", methods=["GET", "POST"])
@login_required
def reaccept_terms():
    # MARSOUD-PRIVACY-TERMS-UPDATE-01 (Batch 9 Ticket 3,
    # 2026-08-01) — template now links to /terms + /privacy
    # instead of dumping full HTML inline, so we no longer need
    # get_terms_html / get_privacy_html here.
    from app.services.legal import get_terms_version
    current = get_terms_version()
    if request.method == "POST":
        if request.form.get("agree_terms") != "on":
            flash("لازم توافق على الشروط للمتابعة.", "error")
            return redirect(url_for("auth.reaccept_terms"))
        current_user.terms_accepted_at = datetime.utcnow()
        current_user.terms_version = current
        # MARSOUD-CONSENT-AUDIT-LOG.
        from app.services.legal import record_consent
        from flask import g as _g
        _cid = _g.get("active_company").id if _g.get("active_company") else None
        record_consent(current_user, source="reaccept",
                        company_id=_cid,
                        document_version=current, request=request)
        db.session.commit()
        flash("تم تسجيل موافقتك على الإصدار الجديد.", "success")
        return redirect(url_for("dashboard.index"))
    return render_template(
        "auth/reaccept.html",
        version=current,
    )


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


@bp.route("/logout", methods=["GET", "POST"])
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
