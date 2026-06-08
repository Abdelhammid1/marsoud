"""HR blueprint — departments + HR-focused employee directory.

All routes are gated by @hr_required (OWNER / ADMIN / HR_MANAGER) per HR-04.
The existing payroll employee CRUD continues to live in routes/payroll.py;
this blueprint adds the people-centric views (departments, HR directory).
"""
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, g
from flask_login import login_required
from app import db
from app.models import Department, Employee, EmployeeStatus
from app.services.hr import (
    create_department, update_department, delete_or_archive_department, HRError,
)
from app.services.permissions import hr_required


bp = Blueprint("hr", __name__)


@bp.route("/")
@login_required
@hr_required
def index():
    """HR home — directory + department summary."""
    cid = g.active_company.id
    employees = Employee.query.filter_by(
        company_id=cid, status=EmployeeStatus.ACTIVE
    ).order_by(Employee.name).all()
    departments = Department.query.filter_by(
        company_id=cid, is_active=True
    ).order_by(Department.name).all()
    return render_template("hr/index.html",
                           employees=employees, departments=departments)


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
