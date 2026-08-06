"""HR blueprint — departments, HR directory, leave (types/balances/requests),
attendance exceptions.

All routes are gated by @hr_required (OWNER / ADMIN / HR_MANAGER) per HR-04.
Employee lifecycle CRUD (HR-02 fields) still lives in routes/payroll.py since
the form already existed there; this blueprint owns the people-side surface.
"""
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal
from flask import Blueprint, render_template, redirect, url_for, flash, request, g
from flask_login import login_required, current_user
from app import db
from app.models import (
    Department, Employee, EmployeeStatus,
    LeaveType, LeaveBalance,
    AttendanceException, AttendanceExceptionType,
    LeaveRequest, LeaveRequestStatus,
)
from app.services.hr import (
    create_department, update_department, delete_or_archive_department, HRError,
)
from app.services.leave import (
    seed_default_leave_types, create_leave_type, update_leave_type,
    ensure_employee_balances,
    create_exception, delete_exception, exceptions_in_period,
    submit_leave_request, approve_leave_request,
    reject_leave_request, cancel_leave_request,
    LeaveError,
)
from app.services.attendance import AttendanceError
from app.services.permissions import hr_required


bp = Blueprint("hr", __name__)


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


@bp.route("/")
@login_required
@hr_required
def index():
    """HR home — directory + department summary + expiring-contracts widget."""
    from datetime import timedelta as _td
    cid = g.active_company.id
    employees = Employee.query.filter_by(
        company_id=cid, status=EmployeeStatus.ACTIVE
    ).order_by(Employee.name).all()
    departments = Department.query.filter_by(
        company_id=cid, is_active=True
    ).order_by(Department.name).all()

    # HR-03 dashboard widget — active employees with contracts ending in ≤60 days
    today = date.today()
    horizon = today + _td(days=60)
    expiring = []
    for e in employees:
        if e.contract_end_date and today <= e.contract_end_date <= horizon:
            days_left = (e.contract_end_date - today).days
            severity = "red" if days_left <= 30 else "amber"
            expiring.append((e, days_left, severity))
    expiring.sort(key=lambda r: r[1])
    return render_template("hr/index.html",
                           employees=employees, departments=departments,
                           expiring=expiring)


# ─── Departments ─────────────────────────────────────────────────────────
@bp.route("/departments")
@login_required
@hr_required
def departments():
    cid = g.active_company.id
    rows = Department.query.filter_by(company_id=cid).order_by(
        Department.is_active.desc(), Department.name
    ).all()
    return render_template("hr/departments.html", departments=rows)


@bp.route("/departments/new", methods=["GET", "POST"])
@login_required
@hr_required
def department_new():
    cid = g.active_company.id
    employees = Employee.query.filter_by(
        company_id=cid, status=EmployeeStatus.ACTIVE
    ).order_by(Employee.name).all()
    if request.method == "POST":
        try:
            mgr_raw = request.form.get("manager_employee_id") or None
            mgr_id = int(mgr_raw) if mgr_raw else None
            create_department(
                cid,
                request.form.get("name", ""),
                description=request.form.get("description", ""),
                manager_employee_id=mgr_id,
            )
            flash("تم إنشاء القسم", "success")
            return redirect(url_for("hr.departments"))
        except (HRError, ValueError) as e:
            flash(str(e), "error")
    return render_template("hr/department_form.html",
                           department=None, employees=employees)


@bp.route("/departments/<int:department_id>/edit", methods=["GET", "POST"])
@login_required
@hr_required
def department_edit(department_id):
    d = db.session.get(Department, department_id)
    if not d or d.company_id != g.active_company.id:
        flash("القسم غير موجود", "error")
        return redirect(url_for("hr.departments"))
    employees = Employee.query.filter_by(
        company_id=g.active_company.id, status=EmployeeStatus.ACTIVE
    ).order_by(Employee.name).all()
    if request.method == "POST":
        try:
            mgr_raw = request.form.get("manager_employee_id") or None
            mgr_id = int(mgr_raw) if mgr_raw else None
            update_department(
                d,
                name=request.form.get("name", ""),
                description=request.form.get("description", ""),
                manager_employee_id=mgr_id,
                is_active=("is_active" in request.form),
            )
            flash("تم حفظ تعديلات القسم", "success")
            return redirect(url_for("hr.departments"))
        except (HRError, ValueError) as e:
            flash(str(e), "error")
    return render_template("hr/department_form.html",
                           department=d, employees=employees)


@bp.route("/departments/<int:department_id>/delete", methods=["POST"])
@login_required
@hr_required
def department_delete(department_id):
    d = db.session.get(Department, department_id)
    if not d or d.company_id != g.active_company.id:
        flash("القسم غير موجود", "error")
        return redirect(url_for("hr.departments"))
    action, n = delete_or_archive_department(d)
    if action == "archived":
        flash(f"تم أرشفة القسم لأنه يحتوي على {n} موظف — لم يُحذف نهائياً", "warning")
    else:
        flash("تم حذف القسم", "success")
    return redirect(url_for("hr.departments"))


# ─── HR-05: Leave types ──────────────────────────────────────────────────
@bp.route("/leave-types")
@login_required
@hr_required
def leave_types():
    cid = g.active_company.id
    # Lazy auto-seed for companies that pre-date HR-05 (or have never set any up).
    if LeaveType.query.filter_by(company_id=cid).count() == 0:
        seed_default_leave_types(cid)
    rows = LeaveType.query.filter_by(company_id=cid).order_by(
        LeaveType.is_active.desc(), LeaveType.name,
    ).all()
    return render_template("hr/leave_types.html", leave_types=rows,
                           current_year=date.today().year)


@bp.route("/leave-types/new", methods=["GET", "POST"])
@login_required
@hr_required
def leave_type_new():
    if request.method == "POST":
        try:
            create_leave_type(
                g.active_company.id,
                name=request.form.get("name", ""),
                accrual_per_month=request.form.get("accrual_per_month", 0),
                max_balance=request.form.get("max_balance", 0),
                is_paid=request.form.get("is_paid") == "1",
            )
            flash("تم إنشاء نوع الإجازة", "success")
            return redirect(url_for("hr.leave_types"))
        except (LeaveError, ValueError) as e:
            flash(str(e), "error")
    return render_template("hr/leave_type_form.html", leave_type=None)


@bp.route("/leave-types/<int:lt_id>/edit", methods=["GET", "POST"])
@login_required
@hr_required
def leave_type_edit(lt_id):
    lt = db.session.get(LeaveType, lt_id)
    if not lt or lt.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("hr.leave_types"))
    if request.method == "POST":
        try:
            update_leave_type(
                lt,
                name=request.form.get("name"),
                accrual_per_month=request.form.get("accrual_per_month"),
                max_balance=request.form.get("max_balance"),
                is_paid=(request.form.get("is_paid") == "1"),
                is_active=("is_active" in request.form),
            )
            flash("تم الحفظ", "success")
            return redirect(url_for("hr.leave_types"))
        except (LeaveError, ValueError) as e:
            flash(str(e), "error")
    return render_template("hr/leave_type_form.html", leave_type=lt)


@bp.route("/leave-types/<int:lt_id>/apply-to-all", methods=["POST"])
@login_required
@hr_required
def leave_type_apply_to_all(lt_id):
    """MARSOUD — bulk-grant the leave type's max_balance to every active
    employee. Closes the loop on Abdelhamid's 'I bumped the max but
    nobody can book' complaint."""
    from app.services.leave import apply_max_balance_to_all
    lt = db.session.get(LeaveType, lt_id)
    if not lt or lt.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("hr.leave_types"))
    year = int(request.form.get("year") or date.today().year)
    n = apply_max_balance_to_all(lt, year=year, actor_id=current_user.id)
    if n:
        flash(
            f"تم تحديث رصيد {n} موظف على نوع \"{lt.name}\" إلى "
            f"{float(lt.max_balance):.2f} يوم لسنة {year}",
            "success",
        )
    else:
        flash(
            f"لا يوجد موظف نشط يحتاج تحديث — الرصيد الحالي ≥ {float(lt.max_balance):.2f}",
            "info",
        )
    return redirect(url_for("hr.leave_types"))


# ─── HR-05: per-employee balances ────────────────────────────────────────
@bp.route("/employees/<int:employee_id>/leave-balances")
@login_required
@hr_required
def employee_balances(employee_id):
    emp = db.session.get(Employee, employee_id)
    if not emp or emp.company_id != g.active_company.id:
        flash("الموظف غير موجود", "error")
        return redirect(url_for("hr.index"))
    year = int(request.args.get("year", date.today().year))
    ensure_employee_balances(emp, year=year)
    balances = LeaveBalance.query.filter_by(
        employee_id=emp.id, year=year
    ).all()
    requests_ = LeaveRequest.query.filter_by(
        employee_id=emp.id
    ).order_by(LeaveRequest.created_at.desc()).limit(50).all()
    return render_template("hr/employee_balances.html",
                           employee=emp, balances=balances,
                           year=year, requests=requests_)


@bp.route("/employees/<int:employee_id>/leave-balances/grant",
          methods=["POST"])
@login_required
@hr_required
def grant_leave_balance(employee_id):
    """MARSOUD — manual balance adjustment (Abdelhamid's complaint:
    'I raised the max but the balance is still 0'). Caps to max_balance."""
    from app.services.leave import set_leave_balance
    emp = db.session.get(Employee, employee_id)
    if not emp or emp.company_id != g.active_company.id:
        flash("الموظف غير موجود", "error")
        return redirect(url_for("hr.index"))
    try:
        lt_id = int(request.form.get("leave_type_id"))
        year = int(request.form.get("year") or date.today().year)
        balance_raw = (request.form.get("balance_days") or "").strip()
        balance = float(balance_raw)
    except (TypeError, ValueError):
        flash("القيم غير صالحة", "error")
        return redirect(url_for("hr.employee_balances",
                                 employee_id=emp.id))
    lt = db.session.get(LeaveType, lt_id)
    if not lt or lt.company_id != g.active_company.id:
        flash("نوع الإجازة غير موجود", "error")
        return redirect(url_for("hr.employee_balances",
                                 employee_id=emp.id))
    set_leave_balance(emp, lt, year, balance_days=balance,
                       actor_id=current_user.id)
    flash(
        f"تم تحديث رصيد {lt.name} للموظف {emp.name} إلى {balance:.2f} يوم",
        "success",
    )
    return redirect(url_for("hr.employee_balances",
                             employee_id=emp.id, year=year))


# ─── HR-05b: attendance exceptions ───────────────────────────────────────
@bp.route("/attendance")
@login_required
@hr_required
def attendance():
    """Monthly calendar view for the whole company (or one filtered employee)."""
    cid = g.active_company.id
    today = date.today()
    year = int(request.args.get("year", today.year))
    month = int(request.args.get("month", today.month))
    emp_id_raw = request.args.get("employee_id") or None
    emp_id = int(emp_id_raw) if emp_id_raw else None

    # include_cancelled — the audit trail is the reason this screen
    # exists; a cancelled row shows struck through and marked ملغى.
    exceptions = exceptions_in_period(cid, year, month, employee_id=emp_id,
                                      include_cancelled=True)
    employees = Employee.query.filter_by(
        company_id=cid, status=EmployeeStatus.ACTIVE,
    ).order_by(Employee.name).all()

    # Group by (employee_id, day) for the grid renderer.
    grid = {}
    for ex in exceptions:
        grid.setdefault(ex.employee_id, {})[ex.date.day] = ex
    days_in_month = monthrange(year, month)[1]
    return render_template("hr/attendance.html",
                           employees=employees, grid=grid,
                           year=year, month=month,
                           days_in_month=days_in_month,
                           filter_employee_id=emp_id)


@bp.route("/attendance/new", methods=["GET", "POST"])
@login_required
@hr_required
def attendance_new():
    cid = g.active_company.id
    employees = Employee.query.filter_by(
        company_id=cid, status=EmployeeStatus.ACTIVE,
    ).order_by(Employee.name).all()
    if request.method == "POST":
        try:
            emp_id = int(request.form.get("employee_id"))
            emp = db.session.get(Employee, emp_id)
            if not emp or emp.company_id != cid:
                raise LeaveError("الموظف غير موجود")
            d = _parse_date(request.form.get("date"))
            if not d:
                raise LeaveError("التاريخ مطلوب")
            type_str = request.form.get("type")
            duration_raw = request.form.get("duration_hours") or None
            # MARSOUD-ATTENDANCE-RANGE (Batch 9 Ticket 5,
            # 2026-08-01) — optional date_to. When provided,
            # bulk-create one exception per non-rest day in the
            # range, skipping days that already have one.
            d_to = _parse_date(request.form.get("date_to"))
            if d_to and d_to != d:
                from app.services.leave import create_exception_range
                summary = create_exception_range(
                    company_id=cid, employee_id=emp_id,
                    date_from=d, date_to=d_to, type_=type_str,
                    note=request.form.get("note"),
                    created_by=current_user.id,
                    rest_weekdays=g.active_company.rest_weekdays,
                )
                msg_parts = [f"تم تسجيل {summary['created']} استثناء"]
                if summary['skipped_weekend']:
                    msg_parts.append(
                        f"تخطينا {summary['skipped_weekend']} "
                        f"يوم أجازة أسبوعية")
                if summary['skipped_existing']:
                    msg_parts.append(
                        f"تخطينا {summary['skipped_existing']} "
                        f"يوم كان مسجّل قبل كده")
                flash(" — ".join(msg_parts), "success")
            else:
                create_exception(
                    company_id=cid, employee_id=emp_id, date_=d,
                    type_=type_str,
                    duration_hours=float(duration_raw) if duration_raw else None,
                    note=request.form.get("note"),
                    created_by=current_user.id,
                )
                flash("تم تسجيل الاستثناء", "success")
            return redirect(url_for("hr.attendance", year=d.year, month=d.month))
        except (LeaveError, ValueError, TypeError) as e:
            flash(str(e), "error")
    return render_template("hr/attendance_form.html",
                           employees=employees, today=date.today().isoformat())


@bp.route("/attendance/<int:ex_id>/delete", methods=["POST"])
@login_required
@hr_required
def attendance_delete(ex_id):
    ex = db.session.get(AttendanceException, ex_id)
    if not ex or ex.company_id != g.active_company.id:
        flash("الاستثناء غير موجود", "error")
        return redirect(url_for("hr.attendance"))
    try:
        # MARSOUD-EXCEPTION-AUDIT — cancelled, not deleted, and the
        # reason is required. The row stays visible marked ملغى.
        delete_exception(ex, reason=request.form.get("cancel_reason"),
                         actor_id=current_user.id)
        flash("تم إلغاء الاستثناء", "success")
    except LeaveError as e:
        flash(str(e), "error")
    return redirect(url_for("hr.attendance",
                            year=ex.date.year, month=ex.date.month))


# ─── HR-06: Leave requests ───────────────────────────────────────────────
# ─── MARSOUD-ATTENDANCE-POLICY (2026-08-05) ─────────────────────────────
# Same shape as the leave-types screens above. Nothing consumes these
# policies yet — ticket 4 is what compares a real check-in against one.
def _policy_form_context():
    """Departments and employees for the scope pickers."""
    cid = g.active_company.id
    from app.models import Department, Employee, EmployeeStatus
    return {
        "departments": Department.query.filter_by(
            company_id=cid, is_active=True).order_by(Department.name).all(),
        "employees": Employee.query.filter_by(
            company_id=cid, status=EmployeeStatus.ACTIVE
        ).order_by(Employee.name).all(),
    }


def _policy_form_values(form):
    """Parse the submitted form into create/update kwargs.

    Blank times come back as None rather than "" so the service's own
    validation produces the Arabic message instead of a ValueError.
    """
    from datetime import time as _time

    def _t(name):
        raw = (form.get(name) or "").strip()
        if not raw:
            return None
        try:
            hh, mm = raw.split(":")[:2]
            return _time(int(hh), int(mm))
        except (ValueError, TypeError):
            raise AttendanceError("صيغة الوقت غير صحيحة")

    days = form.getlist("work_days")
    hours = (form.get("required_hours_per_day") or "").strip()
    return {
        "scope": form.get("scope"),
        "policy_type": form.get("policy_type"),
        "department_id": form.get("department_id", type=int),
        "employee_id": form.get("employee_id", type=int),
        "start_time": _t("start_time"),
        "end_time": _t("end_time"),
        "work_days": ",".join(d for d in days if d.isdigit()) or None,
        "earliest_checkin": _t("earliest_checkin"),
        "latest_checkin": _t("latest_checkin"),
        "required_hours_per_day": float(hours) if hours else None,
        "auto_absent_enabled": "auto_absent_enabled" in form,
    }


@bp.route("/attendance-policies")
@login_required
@hr_required
def attendance_policies():
    from app.services.attendance import policies_for_company
    return render_template(
        "hr/attendance_policies.html",
        policies=policies_for_company(g.active_company.id))


@bp.route("/attendance-policies/new", methods=["GET", "POST"])
@login_required
@hr_required
def attendance_policy_new():
    from app.services.attendance import create_policy
    if request.method == "POST":
        try:
            create_policy(company_id=g.active_company.id,
                          created_by=current_user.id,
                          **_policy_form_values(request.form))
            flash("تم إنشاء سياسة الدوام", "success")
            return redirect(url_for("hr.attendance_policies"))
        except (AttendanceError, ValueError) as e:
            flash(str(e), "error")
    return render_template("hr/attendance_policy_form.html", policy=None,
                           **_policy_form_context())


@bp.route("/attendance-policies/<int:policy_id>/edit", methods=["GET", "POST"])
@login_required
@hr_required
def attendance_policy_edit(policy_id):
    from app.models import AttendancePolicy
    from app.services.attendance import update_policy
    p = db.session.get(AttendancePolicy, policy_id)
    if not p or p.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("hr.attendance_policies"))
    if request.method == "POST":
        try:
            values = _policy_form_values(request.form)
            values["is_active"] = "is_active" in request.form
            update_policy(p, **values)
            flash("تم الحفظ", "success")
            return redirect(url_for("hr.attendance_policies"))
        except (AttendanceError, ValueError) as e:
            flash(str(e), "error")
    return render_template("hr/attendance_policy_form.html", policy=p,
                           **_policy_form_context())


@bp.route("/attendance-policies/<int:policy_id>/delete", methods=["POST"])
@login_required
@hr_required
def attendance_policy_delete(policy_id):
    from app.models import AttendancePolicy
    from app.services.attendance import delete_policy
    p = db.session.get(AttendancePolicy, policy_id)
    if not p or p.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("hr.attendance_policies"))
    delete_policy(p)
    flash("تم حذف السياسة", "success")
    return redirect(url_for("hr.attendance_policies"))


@bp.route("/leave-requests")
@login_required
@hr_required
def leave_requests():
    cid = g.active_company.id
    status_filter = request.args.get("status", "ALL")
    q = LeaveRequest.query.filter_by(company_id=cid)
    if status_filter and status_filter != "ALL":
        try:
            q = q.filter_by(status=LeaveRequestStatus[status_filter])
        except KeyError:
            pass
    rows = q.order_by(LeaveRequest.created_at.desc()).limit(200).all()
    return render_template("hr/leave_requests.html",
                           requests=rows, status_filter=status_filter,
                           statuses=LeaveRequestStatus)


@bp.route("/leave-requests/new", methods=["GET", "POST"])
@login_required
@hr_required
def leave_request_new():
    cid = g.active_company.id
    employees = Employee.query.filter_by(
        company_id=cid, status=EmployeeStatus.ACTIVE,
    ).order_by(Employee.name).all()
    types = LeaveType.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(LeaveType.name).all()
    if request.method == "POST":
        try:
            emp_id = int(request.form.get("employee_id"))
            lt_id = int(request.form.get("leave_type_id"))
            start = _parse_date(request.form.get("start_date"))
            end = _parse_date(request.form.get("end_date"))
            if not start or not end:
                raise LeaveError("تواريخ البداية والنهاية مطلوبة")
            submit_leave_request(
                company_id=cid, employee_id=emp_id, leave_type_id=lt_id,
                start_date=start, end_date=end,
                reason=request.form.get("reason"),
                created_by=current_user.id,
            )
            flash("تم تقديم الطلب — يحتاج اعتماد", "success")
            return redirect(url_for("hr.leave_requests"))
        except (LeaveError, ValueError, TypeError) as e:
            msg = str(e)
            # If the rejection is the "balance is zero/insufficient" one,
            # point the user at the page where they can actually grant it.
            # The flash macro auto-escapes, so we wrap the link with Markup.
            if "الرصيد غير كافٍ" in msg and emp_id:
                from markupsafe import Markup, escape
                bal_url = url_for("hr.employee_balances", employee_id=emp_id)
                msg = Markup(
                    f'{escape(msg)} — '
                    f'<a href="{escape(bal_url)}" class="underline font-bold">'
                    f'اذهب لصفحة أرصدة الموظف لمنح الرصيد</a>'
                )
            flash(msg, "error")
    return render_template("hr/leave_request_form.html",
                           employees=employees, leave_types=types,
                           today=date.today().isoformat())


@bp.route("/leave-requests/<int:req_id>/approve", methods=["POST"])
@login_required
@hr_required
def leave_request_approve(req_id):
    req = db.session.get(LeaveRequest, req_id)
    if not req or req.company_id != g.active_company.id:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("hr.leave_requests"))
    try:
        _req, n = approve_leave_request(
            req, reviewer_id=current_user.id,
            review_note=request.form.get("review_note"),
        )
        flash(f"تم اعتماد الطلب — أُنشئ {n} استثناء حضور", "success")
    except LeaveError as e:
        flash(str(e), "error")
    return redirect(url_for("hr.leave_requests"))


@bp.route("/leave-requests/<int:req_id>/reject", methods=["POST"])
@login_required
@hr_required
def leave_request_reject(req_id):
    req = db.session.get(LeaveRequest, req_id)
    if not req or req.company_id != g.active_company.id:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("hr.leave_requests"))
    try:
        reject_leave_request(
            req, reviewer_id=current_user.id,
            review_note=request.form.get("review_note"),
        )
        flash("تم رفض الطلب", "success")
    except LeaveError as e:
        flash(str(e), "error")
    return redirect(url_for("hr.leave_requests"))


@bp.route("/leave-requests/<int:req_id>/cancel", methods=["POST"])
@login_required
@hr_required
def leave_request_cancel(req_id):
    req = db.session.get(LeaveRequest, req_id)
    if not req or req.company_id != g.active_company.id:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("hr.leave_requests"))
    try:
        cancel_leave_request(
            req, reviewer_id=current_user.id,
            review_note=request.form.get("review_note"),
        )
        flash("تم إلغاء الطلب — أُعيد الرصيد وحُذفت الاستثناءات المرتبطة", "success")
    except LeaveError as e:
        flash(str(e), "error")
    return redirect(url_for("hr.leave_requests"))


# ─── MARSOUD-VIOLATION-POLICY (2026-08-05) — ticket 6 ────────────────────
# Same shape as the attendance-policies screens above. Nothing consumes
# these policies at read time yet either — attendance_deductions() in
# services/leave.py resolves them and services/payroll.py applies them.
def _vp_form_values(form):
    """Parse the violation-policy form into create/update kwargs. Blank
    numeric fields come back as None so the service applies the "no
    allowance" defaults rather than tripping on an empty string."""
    def _get(name):
        raw = (form.get(name) or "").strip()
        return raw if raw != "" else None
    return {
        "scope": form.get("scope"),
        "department_id": form.get("department_id", type=int),
        "employee_id": form.get("employee_id", type=int),
        "absence_unexcused_deduction_days": _get("absence_unexcused_deduction_days"),
        "absence_excused_deduction_days": _get("absence_excused_deduction_days"),
        "monthly_free_late_minutes": _get("monthly_free_late_minutes"),
        "daily_free_late_minutes_cap": _get("daily_free_late_minutes_cap"),
        "permission_count_per_month": _get("permission_count_per_month"),
        "permission_max_hours": _get("permission_max_hours"),
    }


@bp.route("/violation-policies")
@login_required
@hr_required
def violation_policies():
    from app.services.violation import violation_policies_for_company
    return render_template(
        "hr/violation_policies.html",
        policies=violation_policies_for_company(g.active_company.id))


@bp.route("/violation-policies/new", methods=["GET", "POST"])
@login_required
@hr_required
def violation_policy_new():
    from app.services.violation import create_violation_policy, ViolationError
    if request.method == "POST":
        try:
            create_violation_policy(company_id=g.active_company.id,
                                    created_by=current_user.id,
                                    **_vp_form_values(request.form))
            flash("تم إنشاء سياسة الانتهاكات", "success")
            return redirect(url_for("hr.violation_policies"))
        except (ViolationError, ValueError) as e:
            flash(str(e), "error")
    return render_template("hr/violation_policy_form.html", policy=None,
                           **_policy_form_context())


@bp.route("/violation-policies/<int:policy_id>/edit", methods=["GET", "POST"])
@login_required
@hr_required
def violation_policy_edit(policy_id):
    from app.models import AttendanceViolationPolicy
    from app.services.violation import update_violation_policy, ViolationError
    p = db.session.get(AttendanceViolationPolicy, policy_id)
    if not p or p.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("hr.violation_policies"))
    if request.method == "POST":
        try:
            values = _vp_form_values(request.form)
            values["is_active"] = "is_active" in request.form
            update_violation_policy(p, **values)
            flash("تم الحفظ", "success")
            return redirect(url_for("hr.violation_policies"))
        except (ViolationError, ValueError) as e:
            flash(str(e), "error")
    return render_template("hr/violation_policy_form.html", policy=p,
                           **_policy_form_context())


@bp.route("/violation-policies/<int:policy_id>/delete", methods=["POST"])
@login_required
@hr_required
def violation_policy_delete(policy_id):
    from app.models import AttendanceViolationPolicy
    from app.services.violation import delete_violation_policy
    p = db.session.get(AttendanceViolationPolicy, policy_id)
    if not p or p.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("hr.violation_policies"))
    delete_violation_policy(p)
    flash("تم حذف السياسة", "success")
    return redirect(url_for("hr.violation_policies"))


# ─── Permission requests — mirrors /hr/leave-requests ───────────────────
@bp.route("/permission-requests")
@login_required
@hr_required
def permission_requests():
    from app.models import LatePermissionRequest, PermissionStatus
    cid = g.active_company.id
    status_filter = request.args.get("status", "ALL")
    q = LatePermissionRequest.query.filter_by(company_id=cid)
    if status_filter and status_filter != "ALL":
        try:
            q = q.filter_by(status=PermissionStatus[status_filter])
        except KeyError:
            pass
    rows = q.order_by(LatePermissionRequest.created_at.desc()).limit(200).all()
    return render_template("hr/permission_requests.html",
                           requests=rows, status_filter=status_filter,
                           statuses=PermissionStatus)


@bp.route("/permission-requests/new", methods=["GET", "POST"])
@login_required
@hr_required
def permission_request_new():
    from app.services.violation import (
        submit_permission_request, ViolationError,
    )
    from datetime import time as _time

    cid = g.active_company.id
    employees = Employee.query.filter_by(
        company_id=cid, status=EmployeeStatus.ACTIVE,
    ).order_by(Employee.name).all()

    def _t(name):
        raw = (request.form.get(name) or "").strip()
        if not raw:
            return None
        try:
            hh, mm = raw.split(":")[:2]
            return _time(int(hh), int(mm))
        except (TypeError, ValueError):
            return None

    if request.method == "POST":
        try:
            emp_id = int(request.form.get("employee_id"))
            req_date = _parse_date(request.form.get("request_date"))
            if not req_date:
                raise ViolationError("تاريخ الاستئذان مطلوب")
            submit_permission_request(
                company_id=cid, employee_id=emp_id,
                request_date=req_date,
                hours_count=request.form.get("hours_count"),
                start_time=_t("start_time"),
                end_time=_t("end_time"),
                reason=request.form.get("reason"),
                created_by=current_user.id,
            )
            flash("تم تقديم طلب الاستئذان — يحتاج اعتماد", "success")
            return redirect(url_for("hr.permission_requests"))
        except (ViolationError, ValueError, TypeError) as e:
            flash(str(e), "error")
    return render_template("hr/permission_request_form.html",
                           employees=employees,
                           today=date.today().isoformat())


@bp.route("/permission-requests/<int:req_id>/approve", methods=["POST"])
@login_required
@hr_required
def permission_request_approve(req_id):
    from app.models import LatePermissionRequest
    from app.services.violation import approve_permission_request, ViolationError
    req = db.session.get(LatePermissionRequest, req_id)
    if not req or req.company_id != g.active_company.id:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("hr.permission_requests"))
    try:
        approve_permission_request(
            req, reviewer_id=current_user.id,
            review_note=request.form.get("review_note"))
        flash("تم اعتماد طلب الاستئذان", "success")
    except ViolationError as e:
        flash(str(e), "error")
    return redirect(url_for("hr.permission_requests"))


@bp.route("/permission-requests/<int:req_id>/reject", methods=["POST"])
@login_required
@hr_required
def permission_request_reject(req_id):
    from app.models import LatePermissionRequest
    from app.services.violation import reject_permission_request, ViolationError
    req = db.session.get(LatePermissionRequest, req_id)
    if not req or req.company_id != g.active_company.id:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("hr.permission_requests"))
    try:
        reject_permission_request(
            req, reviewer_id=current_user.id,
            review_note=request.form.get("review_note"))
        flash("تم رفض الطلب", "success")
    except ViolationError as e:
        flash(str(e), "error")
    return redirect(url_for("hr.permission_requests"))


@bp.route("/permission-requests/<int:req_id>/cancel", methods=["POST"])
@login_required
@hr_required
def permission_request_cancel(req_id):
    from app.models import LatePermissionRequest
    from app.services.violation import cancel_permission_request, ViolationError
    req = db.session.get(LatePermissionRequest, req_id)
    if not req or req.company_id != g.active_company.id:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("hr.permission_requests"))
    try:
        cancel_permission_request(
            req, reviewer_id=current_user.id,
            review_note=request.form.get("review_note"))
        flash("تم إلغاء الطلب", "success")
    except ViolationError as e:
        flash(str(e), "error")
    return redirect(url_for("hr.permission_requests"))
