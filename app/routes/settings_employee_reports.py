"""MARSOUD-EMPLOYEE-DAILY-REPORTS — settings page.

Owner-only: pick an admin/manager and check which employees they're
allowed to see reports for. Simple two-select flow — the target admin
first, then the employees. No enum for "role"; we allow anyone with
`employee_reports.view` to be assigned (owner is implicit and never
appears in the list).
"""
from flask import Blueprint, render_template, redirect, url_for, request, g, flash
from flask_login import login_required, current_user
from app import db
from app.services.permissions import require_permission
from app.models import Employee, EmployeeReportAccess, User
from app.models.user import user_companies


bp = Blueprint("settings_employee_reports", __name__)


def _company_admins_and_managers(company_id):
    """Return every user who has ANY role in this company except plain
    'employee' — those are the people who could plausibly need viewer
    access. Owners are excluded (they see everyone by default anyway)."""
    rows = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == company_id) &
            (user_companies.c.role.in_(
                ["admin", "accountant", "hr_manager", "sales_manager",
                 "project_manager", "ceo"]
            ))
        )
    ).fetchall()
    if not rows:
        return []
    uids = [r.user_id for r in rows]
    return User.query.filter(User.id.in_(uids)).order_by(User.full_name).all()


@bp.route("/", methods=["GET"])
@login_required
@require_permission("users.manage")
def index():
    company_id = g.active_company.id
    admins = _company_admins_and_managers(company_id)
    # MARSOUD-EMPLOYEE-ARCHIVE — the permission-grant screen only
    # offers ACTIVE employees. Granting access on a resigned employee
    # is nonsense; their old reports remain readable via URL if
    # someone had them before termination.
    from app.models import EmployeeStatus
    employees = Employee.query.filter_by(
        company_id=company_id, status=EmployeeStatus.ACTIVE,
    ).order_by(Employee.name).all()

    selected_uid = request.args.get("viewer_user_id", type=int)
    granted_ids = set()
    if selected_uid:
        granted_ids = {
            row.employee_id for row in EmployeeReportAccess.query.filter_by(
                company_id=company_id, viewer_user_id=selected_uid,
            ).all()
        }
    return render_template(
        "settings/employee_reports.html",
        admins=admins, employees=employees,
        selected_uid=selected_uid, granted_ids=granted_ids,
    )


@bp.route("/save", methods=["POST"])
@login_required
@require_permission("users.manage")
def save():
    company_id = g.active_company.id
    viewer_id = request.form.get("viewer_user_id", type=int)
    if not viewer_id:
        flash("اختر المستخدم أولاً.", "error")
        return redirect(url_for("settings_employee_reports.index"))

    # Verify the target belongs to this company (no cross-tenant grant).
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == viewer_id) &
            (user_companies.c.company_id == company_id)
        )
    ).first()
    if not row:
        flash("هذا المستخدم ليس عضواً في الشركة.", "error")
        return redirect(url_for("settings_employee_reports.index"))

    keep_emp_ids = set(request.form.getlist("employee_ids", type=int))

    # Delete removed grants + insert new ones. Simple diff — the volume
    # is tiny (dozens of employees at most).
    existing = {
        r.employee_id: r for r in EmployeeReportAccess.query.filter_by(
            company_id=company_id, viewer_user_id=viewer_id,
        ).all()
    }
    for eid, r in existing.items():
        if eid not in keep_emp_ids:
            db.session.delete(r)
    for eid in keep_emp_ids:
        if eid in existing:
            continue
        # Only accept employees that belong to this company.
        emp = db.session.get(Employee, eid)
        if not emp or emp.company_id != company_id:
            continue
        db.session.add(EmployeeReportAccess(
            company_id=company_id,
            viewer_user_id=viewer_id,
            employee_id=eid,
        ))
    db.session.commit()
    flash("تم حفظ صلاحيات الرؤية.", "success")
    return redirect(url_for(
        "settings_employee_reports.index",
        viewer_user_id=viewer_id,
    ))
