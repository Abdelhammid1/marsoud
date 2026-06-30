from datetime import date, datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, g, send_file
from flask_login import login_required, current_user
from app import db
from app.models import (
    Employee, EmployeeStatus, ContractType, TerminationReason, Gender,
    PayrollRun, PayrollLine, EmployeeAccrual, Department,
)
from app.services.payroll import (
    run_payroll, terminate_employee, settle_accrual, update_employee,
    billable_days_in_period, auto_absence_late_for,
)
from app.services.ledger import LedgerError
from app.services.numbering import next_number
from app.services.permissions import require_permission

bp = Blueprint("payroll", __name__)


@bp.route("/")
@login_required
@require_permission("employees.view")
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))

    status_filter = request.args.get("status", "ACTIVE")
    search = (request.args.get("search") or "").strip()

    q = Employee.query.filter_by(company_id=g.active_company.id)
    if status_filter and status_filter != "ALL":
        try:
            q = q.filter_by(status=EmployeeStatus[status_filter])
        except KeyError:
            pass
    if search:
        like = f"%{search}%"
        q = q.filter(db.or_(Employee.name.ilike(like), Employee.job_title.ilike(like)))
    employees = q.order_by(Employee.name).all()

    runs = PayrollRun.query.filter_by(company_id=g.active_company.id).order_by(
        PayrollRun.period_year.desc(), PayrollRun.period_month.desc()
    ).limit(12).all()
    return render_template(
        "payroll/index.html",
        employees=employees, runs=runs,
        statuses=EmployeeStatus, status_filter=status_filter, search=search,
    )


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _hr_form_context(employee=None):
    """Build dropdown options for the HR fields on the employee form."""
    cid = g.active_company.id
    departments = Department.query.filter_by(
        company_id=cid, is_active=True
    ).order_by(Department.name).all()
    managers_q = Employee.query.filter_by(
        company_id=cid, status=EmployeeStatus.ACTIVE
    ).order_by(Employee.name)
    # MARSOUD-37 — role picker on the employee form. Exclude owner
    # (per-company singleton) and client (customer portal only). Order:
    # employee first (the default), then alphabetical.
    from app.models import Role
    all_roles = Role.query.filter_by(company_id=cid).filter(
        ~Role.code.in_(("owner", "client"))
    ).order_by(Role.type.asc(), Role.name_ar.asc()).all()
    available_roles = sorted(
        all_roles, key=lambda r: (0 if r.code == "employee" else 1, r.name_ar)
    )
    return {
        "departments": departments,
        "possible_managers": managers_q.all(),
        "available_roles": available_roles,
    }


@bp.route("/employees/new", methods=["GET", "POST"])
@login_required
@require_permission("payroll.employees")
def new_employee():
    if request.method == "POST":
        try:
            ct_str = request.form.get("contract_type", "FULL_TIME")
            start_str = request.form.get("start_date") or date.today().isoformat()
            gender_str = request.form.get("gender") or None
            dept_raw = request.form.get("department_id") or None
            mgr_raw = request.form.get("manager_id") or None
            emp = Employee(
                company_id=g.active_company.id,
                employee_number=next_number(g.active_company.id, "EMPLOYEE"),
                name=request.form.get("name", "").strip(),
                email=request.form.get("email", "").strip(),
                phone=request.form.get("phone", "").strip(),
                job_title=request.form.get("job_title", "").strip(),
                start_date=datetime.strptime(start_str, "%Y-%m-%d").date(),
                contract_type=ContractType[ct_str],
                status=EmployeeStatus.ACTIVE,
                basic_salary=float(request.form.get("basic_salary", 0)),
                allowances=float(request.form.get("allowances", 0)),
                deductions=float(request.form.get("deductions", 0)),
                department_id=int(dept_raw) if dept_raw else None,
                manager_id=int(mgr_raw) if mgr_raw else None,
                national_id=(request.form.get("national_id") or "").strip() or None,
                nationality=(request.form.get("nationality") or "").strip() or None,
                date_of_birth=_parse_date(request.form.get("date_of_birth")),
                gender=Gender[gender_str] if gender_str else None,
                contract_end_date=_parse_date(request.form.get("contract_end_date")),
                notes=(request.form.get("notes") or "").strip() or None,
            )
            if not emp.name:
                raise ValueError("اسم الموظف مطلوب")
            if not emp.email:
                raise ValueError("البريد الإلكتروني مطلوب")
            db.session.add(emp)
            db.session.flush()
            # MARSOUD-COA-REBUILD — open the employee's sub-account under
            # 2130 so payroll postings land per-employee from day 1.
            from app.services.subsidiary import ensure_employee_account
            ensure_employee_account(emp)
            db.session.commit()
            # HR-05 — give the new employee an empty balance row for every active leave type
            try:
                from app.services.leave import ensure_employee_balances
                ensure_employee_balances(emp)
            except Exception:
                from flask import current_app
                current_app.logger.exception("ensure_employee_balances failed")
            # HR-SS — auto-provision a User account (PENDING).
            try:
                from app.services.hr_self_service import ensure_user_for_employee
                picked_role = (request.form.get("user_role_code") or "employee").strip()
                # Defensive: never let owner/client be picked from the form.
                if picked_role in ("owner", "client"):
                    picked_role = "employee"
                user, created = ensure_user_for_employee(
                    emp, actor_id=current_user.id,
                    role_code=picked_role,
                )
                if created:
                    if picked_role != "employee":
                        flash(
                            f"تم إنشاء حساب الموظف بدور '{picked_role}' بحالة PENDING — يحتاج للتفعيل من المالك.",
                            "info",
                        )
                    else:
                        flash(
                            "تم إنشاء حساب الموظف بحالة PENDING — يحتاج للتفعيل من المالك.",
                            "info",
                        )
                else:
                    flash("تم ربط الموظف بحساب مستخدم موجود.", "info")
            except ValueError as e:
                flash(str(e), "error")
            except Exception:
                from flask import current_app
                current_app.logger.exception("ensure_user_for_employee failed")
            flash(f"تم إضافة الموظف {emp.employee_number}", "success")
            return redirect(url_for("payroll.employee_profile", employee_id=emp.id))
        except (ValueError, KeyError) as e:
            flash(str(e), "error")
    return render_template("payroll/employee_form.html",
                           contract_types=ContractType, **_hr_form_context())


@bp.route("/employees/<int:employee_id>")
@login_required
@require_permission("employees.view")
def employee_profile(employee_id):
    emp = db.session.get(Employee, employee_id)
    if not emp or emp.company_id != g.active_company.id:
        return redirect(url_for("payroll.index"))
    payslips = PayrollLine.query.filter_by(employee_id=emp.id).join(PayrollRun).order_by(
        PayrollRun.period_year.desc(), PayrollRun.period_month.desc()
    ).all()
    open_accruals = EmployeeAccrual.query.filter_by(
        employee_id=emp.id, settled_at=None,
    ).order_by(EmployeeAccrual.created_at).all()
    settled_accruals = EmployeeAccrual.query.filter(
        EmployeeAccrual.employee_id == emp.id,
        EmployeeAccrual.settled_at.isnot(None),
    ).order_by(EmployeeAccrual.settled_at.desc()).limit(20).all()
    outstanding = sum(float(a.amount) for a in open_accruals)
    return render_template(
        "payroll/employee_profile.html",
        employee=emp, payslips=payslips,
        termination_reasons=TerminationReason,
        open_accruals=open_accruals, settled_accruals=settled_accruals,
        outstanding=outstanding,
    )


@bp.route("/employees/<int:employee_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("payroll.employees")
def edit_employee(employee_id):
    emp = db.session.get(Employee, employee_id)
    if not emp or emp.company_id != g.active_company.id:
        return redirect(url_for("payroll.index"))
    has_history = bool(list(emp.payroll_lines))
    if request.method == "POST":
        try:
            update_employee(emp, request.form, changed_by_id=current_user.id)
            flash("تم حفظ تعديلات الموظف", "success")
            return redirect(url_for("payroll.employee_profile", employee_id=emp.id))
        except (ValueError, KeyError) as e:
            flash(str(e), "error")
    return render_template(
        "payroll/employee_form.html",
        contract_types=ContractType, statuses=EmployeeStatus,
        employee=emp, has_history=has_history,
        **_hr_form_context(emp),
    )


@bp.route("/accruals/<int:accrual_id>/settle", methods=["POST"])
@login_required
@require_permission("payroll.accruals")
def settle_accrual_route(accrual_id):
    a = db.session.get(EmployeeAccrual, accrual_id)
    if not a or a.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("payroll.index"))
    if a.is_settled:
        flash("تم سداد هذا المبلغ مسبقاً", "warning")
        return redirect(url_for("payroll.employee_profile", employee_id=a.employee_id))
    try:
        pay_via = request.form.get("payment_account_code", "1110")
        settle_accrual(a, payment_method_account_code=pay_via, created_by=current_user.id)
        flash(f"تم سداد {float(a.amount):.2f}", "success")
    except LedgerError as e:
        flash(str(e), "error")
    return redirect(url_for("payroll.employee_profile", employee_id=a.employee_id))


@bp.route("/employees/<int:employee_id>/terminate", methods=["POST"])
@login_required
@require_permission("payroll.employees")
def terminate(employee_id):
    emp = db.session.get(Employee, employee_id)
    if not emp or emp.company_id != g.active_company.id:
        return redirect(url_for("payroll.index"))
    try:
        reason = TerminationReason[request.form.get("reason", "OTHER")]
        notes = request.form.get("notes", "")
        td_str = request.form.get("termination_date") or date.today().isoformat()
        td = datetime.strptime(td_str, "%Y-%m-%d").date()
        terminate_employee(emp, reason, termination_date=td, notes=notes)
        flash("تم تسجيل إنهاء العقد", "success")
    except (KeyError, ValueError) as e:
        flash(str(e), "error")
    return redirect(url_for("payroll.employee_profile", employee_id=emp.id))


@bp.route("/run", methods=["GET", "POST"])
@login_required
@require_permission("payroll.run")
def run():
    """GET: show form with per-employee variable inputs. POST: execute."""
    if not g.active_company:
        return redirect(url_for("companies.new"))

    employees = Employee.query.filter_by(
        company_id=g.active_company.id, status=EmployeeStatus.ACTIVE
    ).order_by(Employee.name).all()

    if request.method == "POST":
        today = date.today()
        year = int(request.form.get("year", today.year))
        month = int(request.form.get("month", today.month))
        send_emails = request.form.get("send_emails") == "1"

        line_inputs = {}
        for emp in employees:
            line_inputs[emp.id] = {
                "working_days": int(request.form.get(f"working_days_{emp.id}", 30) or 30),
                "overtime": float(request.form.get(f"overtime_{emp.id}", 0) or 0),
                "overtime_hours": float(request.form.get(f"overtime_hours_{emp.id}", 0) or 0),
                "absence_days": int(float(request.form.get(f"absence_days_{emp.id}", 0) or 0)),
                "bonus": float(request.form.get(f"bonus_{emp.id}", 0) or 0),
                "absence": float(request.form.get(f"absence_{emp.id}", 0) or 0),
                "late": float(request.form.get(f"late_{emp.id}", 0) or 0),
                "advance": float(request.form.get(f"advance_{emp.id}", 0) or 0),
                "amount_paid": request.form.get(f"amount_paid_{emp.id}", "").strip(),
            }
        # Validate amount_paid not empty
        for emp in employees:
            if line_inputs[emp.id]["amount_paid"] == "":
                flash(f"يجب إدخال المبلغ المدفوع للموظف {emp.name}", "error")
                return redirect(request.url)
            line_inputs[emp.id]["amount_paid"] = line_inputs[emp.id]["amount_paid"] or None
        try:
            pr = run_payroll(
                g.active_company.id, year, month,
                line_inputs=line_inputs, created_by=current_user.id,
                send_emails=send_emails,
            )
            flash(f"تم تنفيذ كشف {pr.number} — صافي {pr.total_net:.2f}", "success")
            return redirect(url_for("payroll.view_run", run_id=pr.id))
        except LedgerError as e:
            flash(str(e), "error")

    today = date.today()
    # Prefill the form for "this month" (or whatever was in the query string)
    year = int(request.args.get("year", today.year))
    month = int(request.args.get("month", today.month))
    return render_template(
        "payroll/run_form.html",
        employees=employees, today=today,
        year=year, month=month,
        billable_days=billable_days_in_period,
        auto_attendance=auto_absence_late_for,
    )


@bp.route("/run/<int:run_id>")
@login_required
@require_permission("payroll.view")
def view_run(run_id):
    pr = db.session.get(PayrollRun, run_id)
    if not pr or pr.company_id != g.active_company.id:
        return redirect(url_for("payroll.index"))
    return render_template("payroll/run.html", run=pr)


@bp.route("/run/<int:run_id>/export/<fmt>")
@login_required
@require_permission("payroll.view")
def export_run(run_id, fmt):
    pr = db.session.get(PayrollRun, run_id)
    if not pr or pr.company_id != g.active_company.id:
        return redirect(url_for("payroll.index"))
    from app.services.export import export_payroll_run_pdf, export_payroll_run_excel
    if fmt == "pdf":
        buf = export_payroll_run_pdf(pr)
        return send_file(buf, mimetype="application/pdf",
                         download_name=f"payroll-{pr.period_year}-{pr.period_month:02d}.pdf",
                         as_attachment=True)
    if fmt == "excel":
        buf = export_payroll_run_excel(pr)
        return send_file(buf,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         download_name=f"payroll-{pr.period_year}-{pr.period_month:02d}.xlsx",
                         as_attachment=True)
    return redirect(url_for("payroll.view_run", run_id=run_id))


@bp.route("/run/<int:run_id>/line/<int:line_id>/payslip.pdf")
@login_required
@require_permission("payroll.view")
def payslip_pdf(run_id, line_id):
    line = db.session.get(PayrollLine, line_id)
    if not line or line.run_id != run_id:
        return redirect(url_for("payroll.index"))
    pr = db.session.get(PayrollRun, run_id)
    if pr.company_id != g.active_company.id:
        return redirect(url_for("payroll.index"))
    from app.services.export import export_payslip_pdf
    buf = export_payslip_pdf(line.employee, line, pr)
    return send_file(buf, mimetype="application/pdf", as_attachment=False,
                     download_name=f"payslip-{line.employee.employee_number}-{pr.period_year}-{pr.period_month:02d}.pdf")
