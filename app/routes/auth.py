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
            if active_companies:
                session["active_company_id"] = active_companies[0].id
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
        password = request.form.get("password", "")
        base_currency = request.form.get("base_currency", "SAR")

        if not email or not password or not full_name or not company_name:
            flash("جميع الحقول مطلوبة", "error")
            return render_template("auth/register.html")

        if User.query.filter_by(email=email).first():
            flash("الإيميل مستخدم بالفعل", "error")
            return render_template("auth/register.html")

        user = User(email=email, full_name=full_name)
        user.set_password(password)
        company = Company(name=company_name, base_currency=base_currency)
        # Bug fix (abdelhamid) — every new company gets the one-month
        # default subscription window + the enterprise plan, instead of
        # being created without any subscription state.
        from app.services.subscription import activate_default_subscription
        activate_default_subscription(company)
        user.companies.append(company)
        db.session.add(user)
        db.session.flush()
        owner_emp = Employee(
            company_id=company.id,
            name=user.full_name,
            email=user.email,
        )
        db.session.add(owner_emp)
        db.session.flush()
        user.employee_id = owner_emp.id
        db.session.commit()
        seed_default_coa(company.id)
        login_user(user)
        session["active_company_id"] = company.id
        flash("تم إنشاء الحساب وشجرة الحسابات الافتراضية", "success")
        return redirect(url_for("dashboard.index"))
    return render_template("auth/register.html")


@bp.route("/logout")
@login_required
def logout():
    end_impersonation()
    logout_user()
    session.pop("active_company_id", None)
    return redirect(url_for("auth.login"))


@bp.route("/switch-company/<int:company_id>")
@login_required
def switch_company(company_id):
    company = db.session.get(Company, company_id)
    if company and company in current_user.companies:
        session["active_company_id"] = company.id
        flash(f"تم التبديل إلى {company.name}", "success")
    return redirect(request.referrer or url_for("dashboard.index"))
