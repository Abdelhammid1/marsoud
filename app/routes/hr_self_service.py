"""HR Self-Service — OWNER activation + employee portal.

Two blueprints:
  - hr_ss_bp     mounted at /hr/accounts — OWNER-only PENDING list.
  - portal_emp_bp mounted at /my — the employee's personal portal.

The portal is intentionally a single endpoint; everything renders into
one page with 4 sections so the employee never has to navigate.
"""
from datetime import date, datetime, timedelta
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g, abort,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    User, Employee, EmployeeHistory, EmployeeChangeType,
    PayrollLine, PayrollRun, EmployeeAccrual,
    LeaveType, LeaveBalance, LeaveRequest, LeaveRequestStatus,
    AdvanceRequest, AdvanceRequestStatus,
)
from app.models.user import user_companies
from app.services.permissions import require_permission, get_user_role
from app.services.hr_self_service import activate_user, disable_user


# ──────────────────────────────────────────────────────────────────────
# OWNER activation page
# ──────────────────────────────────────────────────────────────────────
hr_ss_bp = Blueprint("hr_ss", __name__)


@hr_ss_bp.route("/")
@login_required
@require_permission("users.manage")
def index():
    """Single page where OWNER manages every employee account from inside
    the company panel — no super-admin detour needed.

    Renders 4 buckets:
      1. Employees with NO User account → backfill button + role picker
      2. PENDING accounts → activate / disable
      3. ACTIVE accounts (employees) → resend password link / disable
      4. DISABLED accounts → reactivate
    """
    from app.models import Employee, EmployeeStatus, Role
    cid = g.active_company.id

    # All members of this company.
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == cid)
    ).fetchall()
    user_ids = [r.user_id for r in rows]
    role_by_user = {r.user_id: r.role for r in rows}

    # Bucket 2: PENDING — User exists, is_active=False
    pending = User.query.filter(
        User.id.in_(user_ids),
        User.is_active == False,
        User.status != "DISABLED",
    ).order_by(User.created_at.desc()).all()

    # Bucket 4: DISABLED — explicitly disabled by OWNER
    disabled = User.query.filter(
        User.id.in_(user_ids),
        User.status == "DISABLED",
    ).order_by(User.created_at.desc()).all()

    # MARSOUD-MC-EMPLOYEE — bucketing is per-company now. An owner who
    # has three companies must show as "linked" inside each of them (not
    # only the last one they created). We resolve linkage through
    # Employee.user_id scoped to the active company.
    # MARSOUD-EMPLOYEE-ARCHIVE — restrict linkage to ACTIVE employees
    # so users whose Employee row is TERMINATED disappear from the
    # active-accounts bucket on this page.
    linked_users_here = {
        e.user_id for e in Employee.query.filter(
            Employee.company_id == cid,
            Employee.status == EmployeeStatus.ACTIVE,
            Employee.user_id.isnot(None),
        ).all() if e.user_id is not None
    }

    # Bucket 3: ACTIVE accounts that have an Employee row IN THIS COMPANY.
    active_emps = User.query.filter(
        User.id.in_(user_ids),
        User.is_active == True,
        User.status != "DISABLED",
        User.id.in_(linked_users_here) if linked_users_here else False,
    ).order_by(User.full_name).all()

    # Bucket 1: employees in THIS company without a linked User account.
    unlinked_employees = (
        Employee.query.filter(
            Employee.company_id == cid,
            Employee.status == EmployeeStatus.ACTIVE,
            Employee.user_id.is_(None),
        ).order_by(Employee.name).all()
    )

    # Role picker options for the backfill form. Exclude owner + client.
    available_roles = Role.query.filter_by(company_id=cid).filter(
        ~Role.code.in_(("owner", "client"))
    ).order_by(Role.type.desc(), Role.name_ar.asc()).all()

    return render_template(
        "hr_ss/index.html",
        unlinked_employees=unlinked_employees,
        pending=pending,
        active_emps=active_emps,
        disabled=disabled,
        available_roles=available_roles,
        role_by_user=role_by_user,
    )


@hr_ss_bp.route("/employee/<int:emp_id>/create-user", methods=["POST"])
@login_required
@require_permission("users.manage")
def create_user_for_employee(emp_id):
    """Backfill: create a User account for an existing employee that
    doesn't have one yet. Picks role from the form (default: employee)."""
    from app.models import Employee
    from app.services.hr_self_service import ensure_user_for_employee
    emp = db.session.get(Employee, emp_id)
    if not emp or emp.company_id != g.active_company.id:
        abort(404)
    if not (emp.email or "").strip():
        flash("الموظف ما عندوش إيميل. عدّل بياناته الأول.", "error")
        return redirect(url_for("hr_ss.index"))
    picked_role = (request.form.get("user_role_code") or "employee").strip()
    if picked_role in ("owner", "client"):
        picked_role = "employee"
    try:
        user, created = ensure_user_for_employee(
            emp, actor_id=current_user.id, role_code=picked_role,
        )
        if created:
            flash(
                f"تم إنشاء حساب لـ {emp.name} بدور '{picked_role}' "
                f"وحالة PENDING. اضغط 'تفعيل' لإرسال رابط كلمة السر.",
                "success",
            )
        else:
            flash(
                f"تم ربط {emp.name} بحساب موجود ({user.email}).",
                "info",
            )
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("hr_ss.index"))


@hr_ss_bp.route("/<int:user_id>/set-password", methods=["POST"])
@login_required
@require_permission("users.manage")
def set_password(user_id):
    """Direct password-set — OWNER types the password manually. Used when
    the user can't access email and OWNER wants to dictate the password
    over WhatsApp / SMS / in person."""
    u = db.session.get(User, user_id)
    if not u or not _is_company_member(u.id, g.active_company.id):
        abort(404)
    new_password = request.form.get("new_password", "")
    # MARSOUD-PASSWORD-POLICY — same policy everywhere.
    from app.services.password_policy import validate_password
    ok, reason = validate_password(new_password)
    if not ok:
        flash(reason, "error")
        return redirect(url_for("hr_ss.index"))
    u.set_password(new_password)
    # Owner intentionally typed it → activate the account too.
    from app.models import UserStatus
    u.is_active = True
    u.status = UserStatus.ACTIVE.value
    db.session.commit()
    flash(
        f"تم تعيين كلمة سر جديدة لـ {u.full_name} وتفعيل الحساب.",
        "success",
    )
    return redirect(url_for("hr_ss.index"))


@hr_ss_bp.route("/<int:user_id>/reactivate", methods=["POST"])
@login_required
@require_permission("users.manage")
def reactivate(user_id):
    """Flip a DISABLED account back to ACTIVE without rotating password.
    For temporarily-disabled employees coming back."""
    u = db.session.get(User, user_id)
    if not u or not _is_company_member(u.id, g.active_company.id):
        abort(404)
    from app.models import UserStatus
    u.is_active = True
    u.status = UserStatus.ACTIVE.value
    db.session.commit()
    flash(f"تم إعادة تفعيل حساب {u.full_name}", "success")
    return redirect(url_for("hr_ss.index"))


@hr_ss_bp.route("/<int:user_id>/activate", methods=["POST"])
@login_required
@require_permission("users.manage")
def activate(user_id):
    cid = g.active_company.id
    u = db.session.get(User, user_id)
    if not u or not _is_company_member(u.id, cid):
        abort(404)
    inv, accept_url = activate_user(u, company_id=cid, actor_id=current_user.id)

    # Try sending the email; fall back to flashing the link.
    sent = False
    try:
        from app.services.email import send_invitation_email
        send_invitation_email(inv, accept_url)
        sent = True
    except Exception:
        from flask import current_app
        current_app.logger.exception("activation email send failed")

    if sent:
        flash(f"تم تفعيل {u.full_name} وإرسال إيميل تعيين كلمة السر.", "success")
    else:
        flash(
            f"تم تفعيل {u.full_name}. شارك معه الرابط يدوياً: {accept_url}",
            "info",
        )
    return redirect(url_for("hr_ss.index"))


@hr_ss_bp.route("/<int:user_id>/disable", methods=["POST"])
@login_required
@require_permission("users.manage")
def disable(user_id):
    cid = g.active_company.id
    u = db.session.get(User, user_id)
    if not u or not _is_company_member(u.id, cid):
        abort(404)
    disable_user(u)
    flash(f"تم تعطيل حساب {u.full_name}", "success")
    return redirect(url_for("hr_ss.index"))


def _is_company_member(user_id, company_id):
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == user_id) &
            (user_companies.c.company_id == company_id)
        )
    ).first()
    return row is not None


# ──────────────────────────────────────────────────────────────────────
# Employee personal portal
# ──────────────────────────────────────────────────────────────────────
portal_emp_bp = Blueprint("portal_emp", __name__)


def _my_employee():
    """Resolve the current user's Employee row for the *active* company.

    MARSOUD-MC-EMPLOYEE — a user may own several companies each with its
    own Employee row. The linkage is on Employee.user_id (per-company by
    construction) so we scope by (company_id, user_id) rather than
    following a single scalar column on User.
    """
    return Employee.query.filter_by(
        company_id=g.active_company.id,
        user_id=current_user.id,
    ).first()


def _no_employee_record_response():
    """MARSOUD-PORTAL-403-FIX — what to serve a portal page when the user
    has no Employee row in the active company.

    Redirecting to the dashboard is only safe for roles that can actually
    open it. A user on the `employee` / `client` role gets bounced right
    back here by confine_client_to_portal, so the two redirects loop
    forever (ERR_TOO_MANY_REDIRECTS). Those roles get a terminal page
    instead; everyone else keeps the original flash + redirect.
    """
    if get_user_role(current_user.id, g.active_company.id) in (
            "employee", "client"):
        return render_template("portal_emp/no_record.html")
    flash("هذه الصفحة للموظفين المرتبطين بسجل HR فقط.", "warning")
    return redirect(url_for("dashboard.index"))


@portal_emp_bp.route("/")
@login_required
def index():
    """MARSOUD-57.1 — keep /my/ working but redirect to the new unified
    /my/account page so existing bookmarks + emails don't break."""
    return redirect(url_for("portal_emp.account"))


@portal_emp_bp.route("/account")
@login_required
def account():
    emp = _my_employee()
    if not emp:
        return _no_employee_record_response()

    # Payslips — strictly this employee, joined with the run for the period.
    payslips = (
        db.session.query(PayrollLine, PayrollRun)
        .join(PayrollRun, PayrollLine.run_id == PayrollRun.id)
        .filter(PayrollLine.employee_id == emp.id)
        .order_by(PayrollRun.period_year.desc(),
                  PayrollRun.period_month.desc())
        .all()
    )

    # Timeline — EmployeeHistory rows.
    history = list(emp.history)

    # Leaves — balance per type + own requests.
    balances = LeaveBalance.query.filter_by(
        employee_id=emp.id, year=date.today().year,
    ).all()
    types = LeaveType.query.filter_by(
        company_id=emp.company_id, is_active=True,
    ).order_by(LeaveType.name).all()
    bal_by_type = {b.leave_type_id: b for b in balances}
    requests = LeaveRequest.query.filter_by(
        employee_id=emp.id,
    ).order_by(LeaveRequest.created_at.desc()).limit(50).all()

    # MARSOUD-ATTENDANCE-CHECKIN — today's row drives which button shows,
    # and a fresh math challenge is minted per render so the answer on the
    # page is never the one already used.
    from app.services.attendance import checkin_for
    from app.services.bot_guard import generate_math_challenge
    from datetime import date as _date
    today_checkin = checkin_for(emp.id, _date.today())
    math_question = generate_math_challenge()

    # MARSOUD-ADVANCES — current advance balance + own request history.
    # MARSOUD-ADVANCE-INSTALMENTS — plus the instalments themselves, so
    # "المسدد حتى الآن" stops being a subtraction the employee has to
    # take on trust.
    from app.services.advances import active_advance_for, repayments_for
    advance = active_advance_for(emp.id)
    advance_repayments = repayments_for(advance.id) if advance else []
    advance_requests = AdvanceRequest.query.filter_by(
        employee_id=emp.id,
    ).order_by(AdvanceRequest.created_at.desc()).limit(50).all()

    # MARSOUD-57.1 — compute tenure (years + months) from start_date so the
    # unified account page can show "سنة و3 شهور" without doing math in Jinja.
    tenure_label = "—"
    if emp.start_date:
        today = date.today()
        months = (today.year - emp.start_date.year) * 12 + (today.month - emp.start_date.month)
        if today.day < emp.start_date.day:
            months -= 1
        if months < 0:
            tenure_label = "أقل من شهر"
        else:
            years = months // 12
            rem = months % 12
            parts = []
            if years:
                parts.append(f"{years} {'سنة' if years == 1 else 'سنتين' if years == 2 else 'سنوات'}")
            if rem:
                parts.append(f"{rem} {'شهر' if rem == 1 else 'شهرين' if rem == 2 else 'شهور'}")
            tenure_label = " و".join(parts) if parts else "أقل من شهر"

    return render_template(
        "portal_emp/account.html",
        employee=emp, payslips=payslips, history=history,
        balances=balances, leave_types=types,
        bal_by_type=bal_by_type,
        requests=requests,
        statuses=LeaveRequestStatus,
        today_checkin=today_checkin,
        math_question=math_question,
        advance=advance,
        advance_repayments=advance_repayments,
        advance_requests=advance_requests,
        advance_statuses=AdvanceRequestStatus,
        tenure_label=tenure_label,
    )


@portal_emp_bp.route("/account/password", methods=["POST"])
@login_required
def change_password():
    """MARSOUD-57.1 — employee changes their own password. Requires the
    OLD password before the new one — separate endpoint from the
    super-admin set_password flow so the audit trail stays distinct."""
    emp = _my_employee()
    if not emp:
        abort(403)
    old = request.form.get("old_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if not current_user.check_password(old):
        flash("كلمة السر القديمة غير صحيحة.", "error")
        return redirect(url_for("portal_emp.account") + "#password")
    # MARSOUD-PASSWORD-POLICY — one central validator.
    from app.services.password_policy import validate_password
    ok, reason = validate_password(new)
    if not ok:
        flash(reason, "error")
        return redirect(url_for("portal_emp.account") + "#password")
    if new != confirm:
        flash("كلمة السر الجديدة وتأكيدها غير متطابقين.", "error")
        return redirect(url_for("portal_emp.account") + "#password")
    if new == old:
        flash("كلمة السر الجديدة لازم تكون مختلفة عن القديمة.", "error")
        return redirect(url_for("portal_emp.account") + "#password")
    current_user.set_password(new)
    db.session.commit()
    flash("تم تغيير كلمة السر بنجاح.", "success")
    return redirect(url_for("portal_emp.account") + "#password")


@portal_emp_bp.route("/payslip/<int:line_id>.pdf")
@login_required
def payslip_pdf(line_id):
    """Render the payslip PDF — strict scope to the current user's
    Employee row IN THE ACTIVE COMPANY (MARSOUD-MC-EMPLOYEE)."""
    from flask import send_file
    emp = _my_employee()
    if not emp:
        abort(403)
    line = db.session.get(PayrollLine, line_id)
    if not line or line.employee_id != emp.id:
        abort(404)
    run = db.session.get(PayrollRun, line.run_id)
    from app.services.export import export_payslip_pdf
    buf = export_payslip_pdf(line.employee, line, run)
    fname = f"payslip-{line.employee.employee_number}-{run.period_year}-{run.period_month:02d}.pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=False,
                     download_name=fname)


@portal_emp_bp.route("/leave/new", methods=["POST"])
@login_required
def leave_new():
    emp = _my_employee()
    if not emp:
        abort(403)
    try:
        lt_id = int(request.form.get("leave_type_id"))
        sd = datetime.strptime(request.form.get("start_date"), "%Y-%m-%d").date()
        ed = datetime.strptime(request.form.get("end_date"), "%Y-%m-%d").date()
        reason = (request.form.get("reason") or "").strip() or None
        if ed < sd:
            raise ValueError("تاريخ الانتهاء قبل البداية")
        lt = db.session.get(LeaveType, lt_id)
        if not lt or lt.company_id != emp.company_id:
            raise ValueError("نوع الإجازة غير صالح")
        days = (ed - sd).days + 1

        req = LeaveRequest(
            company_id=emp.company_id,
            employee_id=emp.id,
            leave_type_id=lt.id,
            start_date=sd, end_date=ed,
            days_count=days,
            reason=reason,
            status=LeaveRequestStatus.PENDING,
            created_by=current_user.id,
        )
        db.session.add(req)
        db.session.commit()
        # Notify HR managers in the company so the request shows in their inbox.
        try:
            from app.services.opsflow_extras import notify
            from app.models import NotificationKind
            rows = db.session.execute(
                user_companies.select().where(
                    (user_companies.c.company_id == emp.company_id) &
                    (user_companies.c.role.in_(["owner", "admin", "hr_manager"]))
                )
            ).fetchall()
            for r in rows:
                notify(r.user_id, company_id=emp.company_id,
                       kind=NotificationKind.TASK_ASSIGNED,
                       title=f"🌴 طلب إجازة جديد: {emp.name}",
                       body=f"{lt.name} — {days} يوم",
                       # Was url_for("leave.requests") — no such blueprint,
                       # so this raised BuildError, got swallowed by the
                       # except below, and no HR manager ever received the
                       # notification. The real endpoint is hr.leave_requests.
                       link_url=url_for("hr.leave_requests"))
        except Exception:
            from flask import current_app
            current_app.logger.exception("leave request notify failed")
        flash("تم إرسال طلب الإجازة للاعتماد.", "success")
    except (ValueError, TypeError, KeyError) as e:
        flash(str(e), "error")
    # portal_emp.index 302s to /account and drops the fragment on the way.
    return redirect(url_for("portal_emp.account") + "#leaves")


@portal_emp_bp.route("/permission/new", methods=["POST"])
@login_required
def permission_new():
    """MARSOUD-VIOLATION-POLICY (2026-08-05) — ticket 6.

    Employee-side counterpart to hr.permission_request_new. Same shape
    as leave_new: _my_employee() scopes to the active company and 403s
    anyone without an HR record; the service catches invalid caps or
    monthly-count-exceeded and turns them into flash errors.
    """
    from datetime import time as _time
    from app.services.violation import (
        submit_permission_request, ViolationError,
    )
    emp = _my_employee()
    if not emp:
        abort(403)

    def _t(name):
        raw = (request.form.get(name) or "").strip()
        if not raw:
            return None
        try:
            hh, mm = raw.split(":")[:2]
            return _time(int(hh), int(mm))
        except (TypeError, ValueError):
            return None

    try:
        req_date = datetime.strptime(
            request.form.get("request_date"), "%Y-%m-%d").date()
        submit_permission_request(
            company_id=emp.company_id, employee_id=emp.id,
            request_date=req_date,
            hours_count=request.form.get("hours_count"),
            start_time=_t("start_time"),
            end_time=_t("end_time"),
            reason=request.form.get("reason"),
            created_by=current_user.id,
        )
        # Notify HR — same pattern as leave_new.
        try:
            from app.services.opsflow_extras import notify
            from app.models import NotificationKind
            rows = db.session.execute(
                user_companies.select().where(
                    (user_companies.c.company_id == emp.company_id) &
                    (user_companies.c.role.in_(
                        ["owner", "admin", "hr_manager"]))
                )
            ).fetchall()
            for r in rows:
                notify(r.user_id, company_id=emp.company_id,
                       kind=NotificationKind.TASK_ASSIGNED,
                       title=f"🕐 طلب استئذان جديد: {emp.name}",
                       body=f"{req_date.isoformat()} — "
                            f"{request.form.get('hours_count')} ساعة",
                       link_url=url_for("hr.permission_requests"))
        except Exception:
            from flask import current_app
            current_app.logger.exception("permission request notify failed")
        flash("تم إرسال طلب الاستئذان للاعتماد.", "success")
    except (ViolationError, ValueError, TypeError, KeyError) as e:
        flash(str(e), "error")
    return redirect(url_for("portal_emp.account") + "#attendance")


@portal_emp_bp.route("/advance/new", methods=["POST"])
@login_required
def advance_new():
    """MARSOUD-ADVANCES — employee asking for an advance from /my/.

    Same shape as leave_new: the employee acts on themselves only, so
    there's no permission code — _my_employee() scopes to the active
    company and 403s anyone without an HR record.
    """
    emp = _my_employee()
    if not emp:
        abort(403)
    try:
        from app.services.advances import submit_advance_request, AdvanceError
        try:
            submit_advance_request(
                emp.company_id, emp.id,
                request.form.get("amount"),
                reason=request.form.get("reason"),
                created_by=current_user.id,
            )
            flash("تم إرسال طلب السلفة للاعتماد.", "success")
        except AdvanceError as e:
            flash(str(e), "error")
    except (ValueError, TypeError, KeyError) as e:
        flash(str(e), "error")
    return redirect(url_for("portal_emp.account") + "#advances")


# ──────────────────────────────────────────────────────────────────────
# MARSOUD-CASH-CUSTODY-01 (2026-08-07, slice 3) — employee custody portal
# ──────────────────────────────────────────────────────────────────────
@portal_emp_bp.route("/custody")
@login_required
def custody_list():
    """List the employee's cash custodies + pending requests.
    Standalone page (mirrors daily_reports_list) rather than inline
    on /my/account because custodies need per-line receipt uploads
    that the account page has no room for."""
    emp = _my_employee()
    if not emp:
        return _no_employee_record_response()
    from app.models import (
        CashCustody, CashCustodyRequest, CustodyHolderType,
    )
    custodies = CashCustody.query.filter_by(
        company_id=emp.company_id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
    ).order_by(CashCustody.created_at.desc()).limit(50).all()
    requests_ = CashCustodyRequest.query.filter_by(
        company_id=emp.company_id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
    ).order_by(CashCustodyRequest.created_at.desc()).limit(30).all()
    return render_template(
        "portal_emp/custody_list.html",
        custodies=custodies, requests=requests_,
        employee=emp,
    )


@portal_emp_bp.route("/custody/request", methods=["POST"])
@login_required
def custody_request_new():
    """Employee submits a new cash-custody request. Same shape as
    /my/advance/new — service-layer refuses if a pending request or
    an open custody already exists for this employee."""
    emp = _my_employee()
    if not emp:
        abort(403)
    try:
        from app.services.cash_custody import (
            request_custody, CustodyError,
        )
        from app.models import CustodyHolderType
        _due_raw = (request.form.get("needed_by_date") or "").strip()
        due = None
        if _due_raw:
            try:
                due = datetime.strptime(_due_raw, "%Y-%m-%d").date()
            except ValueError:
                due = None
        try:
            request_custody(
                emp.company_id,
                CustodyHolderType.EMPLOYEE, emp.id,
                request.form.get("amount"),
                purpose=request.form.get("purpose"),
                needed_by_date=due,
                created_by=current_user.id,
            )
            flash("تم إرسال طلب العهدة للاعتماد.", "success")
        except CustodyError as e:
            flash(str(e), "error")
    except (ValueError, TypeError, KeyError) as e:
        flash(str(e), "error")
    return redirect(url_for("portal_emp.custody_list"))


@portal_emp_bp.route("/custody/<int:custody_id>")
@login_required
def custody_detail(custody_id):
    """Employee views one of their custodies + the settlement lines
    already uploaded against it. Only the accountant can add lines
    or close the settlement — this is a read-only view + the receipt
    upload button (which posts through /documents/upload)."""
    emp = _my_employee()
    if not emp:
        return _no_employee_record_response()
    from app.models import CashCustody, CustodyHolderType
    custody = CashCustody.query.filter_by(
        id=custody_id, company_id=emp.company_id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
    ).first()
    if not custody:
        abort(404)
    return render_template(
        "portal_emp/custody_detail.html",
        custody=custody, employee=emp,
    )


# ──────────────────────────────────────────────────────────────────────
# MARSOUD-ITEM-CUSTODY-01 (2026-08-07) — physical-item custody portal
# ──────────────────────────────────────────────────────────────────────
@portal_emp_bp.route("/items")
@login_required
def items_list():
    """Items currently held by this employee + their pending requests
    + the list of items they could request (items with no active
    custody)."""
    emp = _my_employee()
    if not emp:
        return _no_employee_record_response()
    from app.models import (
        ItemCustody, ItemCustodyRequest, CustodyHolderType,
    )
    from app.services.item_custody import items_available_for_company
    mine = ItemCustody.query.filter_by(
        company_id=emp.company_id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
    ).order_by(ItemCustody.created_at.desc()).limit(50).all()
    my_requests = ItemCustodyRequest.query.filter_by(
        company_id=emp.company_id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
    ).order_by(ItemCustodyRequest.created_at.desc()).limit(30).all()
    available = items_available_for_company(emp.company_id)
    return render_template(
        "portal_emp/items_list.html",
        custodies=mine, requests=my_requests,
        available=available, employee=emp,
    )


@portal_emp_bp.route("/items/request", methods=["POST"])
@login_required
def items_request_new():
    """Submit a request for one of the available items. Portal has
    no permission decorator — portal_emp is prefix-allowlisted."""
    emp = _my_employee()
    if not emp:
        abort(403)
    try:
        from app.services.item_custody import (
            request_item_custody, ItemCustodyError,
        )
        from app.models import CustodyHolderType
        try:
            request_item_custody(
                emp.company_id,
                item_id=int(request.form.get("item_id") or 0),
                holder_type=CustodyHolderType.EMPLOYEE,
                holder_id=emp.id,
                purpose=request.form.get("purpose"),
                created_by=current_user.id,
            )
            flash("تم إرسال طلب استلام العنصر للاعتماد.", "success")
        except ItemCustodyError as e:
            flash(str(e), "error")
    except (ValueError, TypeError, KeyError) as e:
        flash(str(e), "error")
    return redirect(url_for("portal_emp.items_list"))


@portal_emp_bp.route("/items/<int:custody_id>")
@login_required
def items_detail(custody_id):
    """Employee views one of their item custodies. Read-only —
    settlement is done by the accountant."""
    emp = _my_employee()
    if not emp:
        return _no_employee_record_response()
    from app.models import ItemCustody, CustodyHolderType
    custody = ItemCustody.query.filter_by(
        id=custody_id, company_id=emp.company_id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
    ).first()
    if not custody:
        abort(404)
    return render_template(
        "portal_emp/items_detail.html",
        custody=custody, employee=emp,
    )


# ──────────────────────────────────────────────────────────────────────
# MARSOUD-EMPLOYEE-DAILY-REPORTS — employee-side review + submit
# ──────────────────────────────────────────────────────────────────────
@portal_emp_bp.route("/daily-reports")
@login_required
def daily_reports_list():
    """List every report row owned by the current employee (both DRAFT
    and SUBMITTED). No permission gate — this is intrinsically your own
    data."""
    emp = _my_employee()
    if not emp:
        return _no_employee_record_response()
    from app.models import EmployeeDailyReport
    reports = EmployeeDailyReport.query.filter_by(
        employee_id=emp.id,
    ).order_by(EmployeeDailyReport.report_date.desc()).all()
    return render_template("portal_emp/daily_reports_list.html",
                             emp=emp, reports=reports)


@portal_emp_bp.route("/daily-reports/<int:report_id>",
                        methods=["GET", "POST"])
@login_required
def daily_report_detail(report_id):
    emp = _my_employee()
    if not emp:
        abort(403)
    from app.models import EmployeeDailyReport, DailyReportStatus
    from app.services.daily_digest import submit_report, build_digest
    r = db.session.get(EmployeeDailyReport, report_id)
    if not r or r.employee_id != emp.id:
        abort(404)

    # ASMAA-FIX 2026-07-03 (round 3) — DRAFT bodies are frozen at
    # cron-time. A report built before the readability rewrite deployed
    # still shows the raw "STATUS_CHANGED ← مهمة #61" dump the user
    # can't read. Re-run build_digest on view for any DRAFT — it's
    # cheap, deterministic, and idempotent (the same helper runs
    # nightly). SUBMITTED reports are frozen forever, no refresh.
    if r.status == DailyReportStatus.DRAFT and request.method == "GET":
        try:
            build_digest(r.company_id, r.employee_id, r.report_date)
            db.session.commit()
            db.session.refresh(r)
        except Exception:
            db.session.rollback()

    if request.method == "POST":
        # Ownership already enforced above. Refuse any edit on a
        # SUBMITTED report.
        if r.status == DailyReportStatus.SUBMITTED:
            flash("لا يمكن تعديل تقرير تم توليده نهائياً.", "error")
            return redirect(url_for(
                "portal_emp.daily_report_detail", report_id=r.id,
            ))
        notes = (request.form.get("employee_notes") or "").strip() or None
        r.employee_notes = notes
        if request.form.get("submit_final") == "1":
            submit_report(r.id, current_user.id)
            db.session.commit()
            flash("تم توليد التقرير وإخطار المسؤولين.", "success")
            return redirect(url_for("portal_emp.daily_reports_list"))
        db.session.commit()
        flash("تم حفظ الملاحظات.", "success")
        return redirect(url_for(
            "portal_emp.daily_report_detail", report_id=r.id,
        ))
    return render_template("portal_emp/daily_report_detail.html",
                             emp=emp, report=r)


# ─── MARSOUD-ATTENDANCE-CHECKIN (tickets 2 + 3, 2026-08-05) ─────────────
def _coord(name):
    """A browser coordinate, or None. A refused location permission must
    not block the check-in — the ticket is explicit that location is
    evidence when offered, never a gate."""
    raw = (request.form.get(name) or "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    # Anything outside real coordinates is a broken client, not a place.
    if name.endswith("lat") and not (-90 <= val <= 90):
        return None
    if name.endswith("lng") and not (-180 <= val <= 180):
        return None
    return val


def _attendance_action(kind):
    """Shared body of check-in and check-out.

    The math challenge is verified BEFORE anything is written. It is
    consumed either way, so a wrong answer costs the employee a fresh
    question rather than letting a script retry the same one.
    """
    from app.services.bot_guard import verify_math_challenge
    from app.services.attendance import check_in, check_out, AttendanceError

    emp = _my_employee()
    if not emp:
        abort(403)

    if not verify_math_challenge(request.form.get("math_answer")):
        flash("إجابة السؤال الحسابي غير صحيحة — حاول مرة أخرى.", "error")
        return redirect(url_for("portal_emp.account") + "#attendance")

    lat = _coord("check_lat")
    lng = _coord("check_lng")
    try:
        if kind == "in":
            _row, exc = check_in(emp, lat=lat, lng=lng)
            if exc is not None:
                # Told now, not discovered on the payslip.
                flash("تم تسجيل الحضور — وسُجّل تأخير اليوم تلقائيًا "
                      "حسب سياسة الدوام.", "warning")
            else:
                flash("تم تسجيل الحضور.", "success")
        else:
            check_out(emp, lat=lat, lng=lng)
            flash("تم تسجيل الانصراف.", "success")
    except AttendanceError as e:
        flash(str(e), "error")
    return redirect(url_for("portal_emp.account") + "#attendance")


@portal_emp_bp.route("/attendance/checkin", methods=["POST"])
@login_required
def attendance_checkin():
    return _attendance_action("in")


@portal_emp_bp.route("/attendance/checkout", methods=["POST"])
@login_required
def attendance_checkout():
    return _attendance_action("out")


# ─── MARSOUD-MY-ATTENDANCE (2026-08-05) — ticket 7 ──────────────────────
@portal_emp_bp.route("/attendance")
@login_required
def attendance():
    """One page that answers "what has attendance recorded for me this
    month, and how much margin do I have left?" — read-only.

    Actions live on their existing endpoints: check-in/out and the
    permission button are on /my/account under the same #attendance
    fragment. This page links back to them rather than duplicating.

    Cross-tenant safety: _my_employee() scopes by (active_company_id,
    current_user.id), so every query below is also company-scoped by
    virtue of filtering on the resolved emp.id.
    """
    from app.models import AttendanceCheckin, LatePermissionRequest, PermissionStatus
    from app.services.leave import exceptions_in_period
    from app.services.violation import (
        resolve_violation_policy_for_employee, approved_permissions_for,
    )
    from app.services.payroll import late_month_breakdown

    emp = _my_employee()
    if not emp:
        return _no_employee_record_response()

    today = date.today()
    y, m = today.year, today.month
    month_start = date(y, m, 1)

    checkins = (AttendanceCheckin.query
                .filter_by(employee_id=emp.id)
                .filter(AttendanceCheckin.date >= month_start)
                .order_by(AttendanceCheckin.date.desc()).all())
    exceptions = exceptions_in_period(
        emp.company_id, y, m, employee_id=emp.id)  # active-only by default
    permits = (LatePermissionRequest.query
               .filter_by(employee_id=emp.id)
               .filter(LatePermissionRequest.request_date >= month_start)
               .order_by(LatePermissionRequest.request_date.desc()).all())

    policy = resolve_violation_policy_for_employee(emp.id)

    # Remaining free-late margin + permission count. Both are resolved
    # through the same helper the payroll engine uses so the number the
    # employee reads on this page is exactly the residual payroll would
    # see when it runs — no drift is possible between the two paths.
    remaining_pool = None
    used_permits = 0
    remaining_perms = None
    if policy is not None:
        used_permits = len(approved_permissions_for(emp.id, y, m))
        remaining_perms = max(
            0, int(policy.permission_count_per_month or 0) - used_permits)

        breakdown = late_month_breakdown(emp.id, y, m, policy=policy)
        remaining_pool = int(round(breakdown["pool_remaining"]))

    balances = LeaveBalance.query.filter_by(
        employee_id=emp.id, year=y).all()

    return render_template(
        "portal_emp/attendance.html",
        employee=emp,
        month_label=f"{y}-{m:02d}",
        checkins=checkins,
        exceptions=exceptions,
        permits=permits,
        permit_statuses=PermissionStatus,
        policy=policy,
        remaining_pool=remaining_pool,
        remaining_perms=remaining_perms,
        used_permits=used_permits,
        balances=balances,
    )


# ─── MARSOUD-PORTAL-MY-ACTIVITY-01 (2026-08-06) ─────────────────────────
@portal_emp_bp.route("/activity")
@login_required
def activity():
    """The employee's own login history + actions. Read-only, last 90
    days, active company only.

    The entire security story is one line — the OVERWRITE of user_id
    below _parse_filters(). A crafted ?user_id=5 would otherwise land
    in the filter dict and _apply_filters would answer for that user;
    the overwrite happens after parse and before filter, so no request
    parameter can widen the query beyond the current user.

    Reuses the owner page's _parse_filters + _apply_filters + the
    _activity_page.html partial rather than cloning them — a
    portal-only copy would drift the moment either page grew a new
    filter, and the shared partial already knows every column shape.
    """
    from app.routes.activity_views import _parse_filters, _apply_filters
    from app.models import UserActivityLog, UserSession, ACTION_TYPES
    from datetime import timedelta as _td

    emp = _my_employee()
    if not emp:
        return _no_employee_record_response()

    f = _parse_filters()
    # LOAD-BEARING. Overwrite whatever the URL supplied.
    f["user_id"] = current_user.id

    # 90-day hard floor. The range dropdown maxes at 90d, but a crafted
    # ?from=2020-01-01 would otherwise widen the window; clamp it
    # server-side too so the spec's "آخر 90 يوم" holds regardless of
    # how the request was constructed.
    hard_floor = datetime.utcnow() - _td(days=90)
    if f["_start"] < hard_floor:
        f["_start"] = hard_floor

    activity_q = UserActivityLog.query
    sessions_q = UserSession.query
    activity_q, sessions_q = _apply_filters(
        activity_q, sessions_q, f, company_scope=g.active_company.id)

    activities = activity_q.order_by(
        UserActivityLog.created_at.desc()).limit(500).all()
    sessions = sessions_q.order_by(
        UserSession.login_at.desc()).limit(200).all()

    return render_template(
        "portal_emp/activity.html",
        activities=activities, sessions=sessions,
        users=[], actions=list(ACTION_TYPES), entity_types=[],
        filters=f, is_portal=True,
    )


# ─── MARSOUD-TASK-ARCHIVE-MINE (2026-08-08) ────────────────────────
# Portal mirror for tasks.archive_mine — the `employee` role is
# portal-confined by confine_client_to_portal (app/__init__.py), so
# `/tasks/*` 403s for them. Same service composer + same
# per-user visibility scope; different route + template.
@portal_emp_bp.route("/archive", methods=["GET"])
@login_required
def archive():
    """Employee-role mirror of tasks.archive_mine — their own
    archived tasks (assignee OR m2m member OR creator) only."""
    from app.models import Task  # noqa: F401 (imported for the model registry)
    from app.services.task_archive import my_archived_tasks
    if not g.get("active_company"):
        abort(404)
    rows = my_archived_tasks(
        g.active_company.id, current_user.id).all()
    return render_template("portal_emp/archive.html", tasks=rows)


@portal_emp_bp.route("/archive/<int:task_id>/restore",
                      methods=["POST"])
@login_required
def archive_restore(task_id):
    """Self-restore under /my/*. 404 (not 403) on a stranger's task
    id — keep the portal archive opaque about existence."""
    from app.models import Task
    from app.services.task_archive import (
        unarchive_task, can_restore_mine,
    )
    if not g.get("active_company"):
        abort(404)
    t = db.session.get(Task, task_id)
    if (t is None
        or t.company_id != g.active_company.id
        or not can_restore_mine(t, current_user.id)):
        abort(404)
    unarchive_task(t, actor_id=current_user.id)
    flash(f"↩ تم استعادة المهمة: {t.title}", "success")
    return redirect(url_for("portal_emp.archive"))
