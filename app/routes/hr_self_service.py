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
    cid = g.active_company.id
    # Pull every user who is a member of this company + currently PENDING.
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == cid)
    ).fetchall()
    user_ids = [r.user_id for r in rows]
    pending = User.query.filter(
        User.id.in_(user_ids),
        User.is_active == False,
    ).all()
    # Also expose the count of ACTIVE employee-portal accounts for context.
    active_emp_count = User.query.filter(
        User.id.in_(user_ids),
        User.is_active == True,
        User.employee_id.isnot(None),
    ).count()
    return render_template(
        "hr_ss/index.html",
        pending=pending,
        active_emp_count=active_emp_count,
    )


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
    """Resolve the current user's Employee row (raises 404 if not linked)."""
    if not current_user.employee_id:
        return None
    emp = db.session.get(Employee, current_user.employee_id)
    if not emp or emp.company_id != g.active_company.id:
        return None
    return emp


@portal_emp_bp.route("/")
@login_required
def index():
    emp = _my_employee()
    if not emp:
        flash("هذه الصفحة للموظفين المرتبطين بسجل HR فقط.", "warning")
        return redirect(url_for("dashboard.index"))

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

    return render_template(
        "portal_emp/index.html",
        employee=emp, payslips=payslips, history=history,
        balances=balances, leave_types=types,
        bal_by_type=bal_by_type,
        requests=requests,
        statuses=LeaveRequestStatus,
    )


@portal_emp_bp.route("/payslip/<int:line_id>.pdf")
@login_required
def payslip_pdf(line_id):
    """Render the payslip PDF — strict scope to current user's employee_id."""
    from flask import send_file
    if not current_user.employee_id:
        abort(403)
    line = db.session.get(PayrollLine, line_id)
    if not line or line.employee_id != current_user.employee_id:
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
                       link_url=url_for("leave.requests"))
        except Exception:
            from flask import current_app
            current_app.logger.exception("leave request notify failed")
        flash("تم إرسال طلب الإجازة للاعتماد.", "success")
    except (ValueError, TypeError, KeyError) as e:
        flash(str(e), "error")
    return redirect(url_for("portal_emp.index") + "#leaves")
