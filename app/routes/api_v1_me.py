"""MARSOUD-MOBILE-FLUTTER — JSON `/my/*` mirror for the employee portal.

Every endpoint here is a JSON version of an existing HTML route under
`hr_self_service.portal_emp_bp`. Reuses the SAME service functions so
behaviour matches the web exactly — never re-implement validation.

Mounted at /api/v1/my. Uses the shared `/api/v1/*` bearer + rate-limit
gate installed by the parent `api_v1_bp` before_request, so no local
auth boilerplate here.

Auth-scoping rule (same as web):
  · Every endpoint resolves the current user's Employee row IN THE ACTIVE
    COMPANY via `_my_employee_or_404()`. A user with no Employee row
    gets a 404 with `{error: "no_employee_record"}` — the mobile UI
    shows a terminal screen just like `portal_emp/no_record.html`.
  · Cross-tenant safety comes from `g.active_company` already being
    resolved by api_v1's before_request from `?company_id=N`. No
    endpoint here reads `request.args["company_id"]` directly.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request, g, current_app, send_file, abort
from flask_login import current_user

from app import db
from app.models import (
    Employee, EmployeeStatus,
    PayrollLine, PayrollRun,
    LeaveType, LeaveBalance, LeaveRequest, LeaveRequestStatus,
    AdvanceRequest, AdvanceRequestStatus,
    LatePermissionRequest, PermissionStatus,
    AttendanceCheckin,
    CashCustody, CashCustodyRequest, CustodyHolderType,
    ItemCustody, ItemCustodyRequest,
    EmployeeDailyReport, DailyReportStatus,
    UserActivityLog, UserSession,
    Task,
)
from app.services import api_serializers as S
from app.services.api_guard import install_api_guard


bp = Blueprint("api_v1_me", __name__)
install_api_guard(bp)


# ─── Helpers ──────────────────────────────────────────────────────────
def _err(message, status=400, **extra):
    payload = {"error": message}
    payload.update(extra)
    resp = jsonify(payload)
    resp.status_code = status
    return resp


def _my_employee_or_404():
    """Mirror of `hr_self_service._my_employee()`. Returns the Employee
    row linked to `current_user` in the active company, or None.

    Kept strict: `status == ACTIVE`. A TERMINATED employee should not be
    able to log in and read old data through the mobile app either.
    """
    cid = g.active_company.id if g.get("active_company") else None
    if not cid:
        return None
    return Employee.query.filter_by(
        company_id=cid,
        user_id=current_user.id,
        status=EmployeeStatus.ACTIVE,
    ).first()


def _no_employee():
    return _err("no_employee_record", 404)


def _tenure_label(start_date):
    """Mirror the Arabic 'X سنة و Y شهر' logic from
    hr_self_service.account so the mobile displays the same string."""
    if not start_date:
        return "—"
    today = date.today()
    months = (today.year - start_date.year) * 12 + (
        today.month - start_date.month)
    if today.day < start_date.day:
        months -= 1
    if months < 0:
        return "أقل من شهر"
    years = months // 12
    rem = months % 12
    parts = []
    if years:
        parts.append(
            f"{years} {'سنة' if years == 1 else 'سنتين' if years == 2 else 'سنوات'}")
    if rem:
        parts.append(
            f"{rem} {'شهر' if rem == 1 else 'شهرين' if rem == 2 else 'شهور'}")
    return " و".join(parts) if parts else "أقل من شهر"


def _dec(raw):
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError):
        raise ValueError("قيمة عددية غير صالحة")


def _iso_date(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("تنسيق التاريخ يجب أن يكون YYYY-MM-DD")


def _iso_time(raw):
    from datetime import time as _time
    if not raw:
        return None
    try:
        hh, mm = str(raw).split(":")[:2]
        return _time(int(hh), int(mm))
    except (TypeError, ValueError):
        raise ValueError("تنسيق الوقت يجب أن يكون HH:MM")


def _coord(name, body):
    """Same rule as hr_self_service._coord — refused permission ≠ blocked.
    Silently drop out-of-range values, don't fail the request."""
    raw = body.get(name)
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if name.endswith("lat") and not (-90 <= val <= 90):
        return None
    if name.endswith("lng") and not (-180 <= val <= 180):
        return None
    return val


def _body():
    return request.get_json(silent=True) or request.form or {}


# ─── Account bundle ───────────────────────────────────────────────────
@bp.route("/account", methods=["GET"])
def account():
    """One payload with everything the mobile home screen needs, so the
    initial paint is a single round trip."""
    from app.services.attendance import checkin_for
    from app.services.advances import active_advance_for, repayments_for

    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()

    payslips = (
        db.session.query(PayrollLine, PayrollRun)
        .join(PayrollRun, PayrollLine.run_id == PayrollRun.id)
        .filter(PayrollLine.employee_id == emp.id)
        .order_by(PayrollRun.period_year.desc(),
                  PayrollRun.period_month.desc())
        .all()
    )

    balances = LeaveBalance.query.filter_by(
        employee_id=emp.id, year=date.today().year,
    ).all()
    leave_types = LeaveType.query.filter_by(
        company_id=emp.company_id, is_active=True,
    ).order_by(LeaveType.name).all()
    requests = LeaveRequest.query.filter_by(
        employee_id=emp.id,
    ).order_by(LeaveRequest.created_at.desc()).limit(50).all()

    today_ci = checkin_for(emp.id, date.today())
    advance = active_advance_for(emp.id)
    advance_repayments = repayments_for(advance.id) if advance else []
    advance_requests = AdvanceRequest.query.filter_by(
        employee_id=emp.id,
    ).order_by(AdvanceRequest.created_at.desc()).limit(50).all()

    return jsonify({
        "employee": S.employee_full(emp),
        "tenure_label": _tenure_label(emp.start_date),
        "payslips": [
            S.payroll_line_brief(line, run=run) for line, run in payslips
        ],
        "leave": {
            "types": [S.leave_type_brief(lt) for lt in leave_types],
            "balances": [S.leave_balance_brief(b) for b in balances],
            "requests": [S.leave_request_brief(r) for r in requests],
        },
        "advance": {
            "active": S.advance_brief(advance),
            "repayments": [S.advance_repayment_brief(r) for r in advance_repayments],
            "requests": [S.advance_request_brief(r) for r in advance_requests],
        },
        "today_checkin": S.checkin_brief(today_ci),
    })


# ─── Password change (mobile mirror of /my/account/password) ──────────
@bp.route("/account/password", methods=["POST"])
def change_password():
    from app.services.password_policy import validate_password
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()

    body = _body()
    old = body.get("old") or body.get("old_password") or ""
    new = body.get("new") or body.get("new_password") or ""

    if not current_user.check_password(old):
        return _err("wrong_old_password", 400)
    ok, reason = validate_password(new)
    if not ok:
        return _err(reason, 400)
    if new == old:
        return _err("new_must_differ", 400)

    current_user.set_password(new)
    db.session.commit()
    return jsonify({"ok": True})


# ─── Payslip PDF ──────────────────────────────────────────────────────
@bp.route("/payslip/<int:line_id>", methods=["GET"])
def payslip_pdf(line_id):
    """Returns the same PDF binary the web `/my/payslip/<line>.pdf` does.
    Enforces ownership strictly."""
    from app.services.export import export_payslip_pdf
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    line = db.session.get(PayrollLine, line_id)
    if not line or line.employee_id != emp.id:
        return _err("not_found", 404)
    run = db.session.get(PayrollRun, line.run_id)
    buf = export_payslip_pdf(line.employee, line, run)
    fname = (f"payslip-{line.employee.employee_number}-"
             f"{run.period_year}-{run.period_month:02d}.pdf")
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=False, download_name=fname)


# ─── Leave requests ───────────────────────────────────────────────────
@bp.route("/leave", methods=["GET"])
def leave_list():
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    rows = LeaveRequest.query.filter_by(
        employee_id=emp.id,
    ).order_by(LeaveRequest.created_at.desc()).limit(100).all()
    return jsonify({
        "count": len(rows),
        "requests": [S.leave_request_brief(r) for r in rows],
    })


@bp.route("/leave", methods=["POST"])
def leave_new():
    """CRITICAL — must go through `services.leave.submit_leave_request`,
    which is the single source of truth for the overlap / rest-day /
    balance rules. A prior version of this handler inserted the row by
    hand and skipped every one of those checks (bug parity with the web
    self-service, which had the same gap). The service raises
    LeaveError on any violation, so surface those as 400s."""
    from app.models.user import user_companies
    from app.services.opsflow_extras import notify
    from app.services.leave import submit_leave_request, LeaveError
    from app.models import NotificationKind

    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    body = _body()
    try:
        lt_id = int(body.get("leave_type_id"))
        sd = _iso_date(body.get("start_date"))
        ed = _iso_date(body.get("end_date"))
        reason = (body.get("reason") or "").strip() or None
        if not sd or not ed:
            raise ValueError("start_date + end_date مطلوبين")
        req = submit_leave_request(
            company_id=emp.company_id,
            employee_id=emp.id,
            leave_type_id=lt_id,
            start_date=sd, end_date=ed,
            reason=reason,
            created_by=current_user.id,
        )
        # Notify HR — same fan-out the web endpoint does.
        try:
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
                       title=f"🌴 طلب إجازة جديد: {emp.name}",
                       body=f"{req.leave_type.name} — {req.days_count} يوم",
                       link_url="/hr/leave-requests")
        except Exception:
            current_app.logger.exception("leave request notify failed")
        return jsonify({"ok": True, "request": S.leave_request_brief(req)}), 201
    except (LeaveError, ValueError, TypeError, KeyError) as e:
        return _err(str(e), 400)


# ─── Permission (استئذان) requests ────────────────────────────────────
@bp.route("/permission", methods=["GET"])
def permission_list():
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    rows = LatePermissionRequest.query.filter_by(
        employee_id=emp.id,
    ).order_by(LatePermissionRequest.created_at.desc()).limit(100).all()
    return jsonify({
        "count": len(rows),
        "requests": [S.permission_request_brief(r) for r in rows],
    })


@bp.route("/permission", methods=["POST"])
def permission_new():
    from app.services.violation import (
        submit_permission_request, ViolationError,
    )
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    body = _body()
    try:
        rd = _iso_date(body.get("request_date"))
        if not rd:
            raise ValueError("request_date مطلوب")
        req = submit_permission_request(
            company_id=emp.company_id, employee_id=emp.id,
            request_date=rd,
            hours_count=body.get("hours_count"),
            start_time=_iso_time(body.get("start_time")),
            end_time=_iso_time(body.get("end_time")),
            reason=body.get("reason"),
            created_by=current_user.id,
        )
        return jsonify({"ok": True,
                        "request": S.permission_request_brief(req)}), 201
    except (ViolationError, ValueError, TypeError, KeyError) as e:
        return _err(str(e), 400)


# ─── Advance requests ─────────────────────────────────────────────────
@bp.route("/advance", methods=["GET"])
def advance_list():
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    rows = AdvanceRequest.query.filter_by(
        employee_id=emp.id,
    ).order_by(AdvanceRequest.created_at.desc()).limit(100).all()
    return jsonify({
        "count": len(rows),
        "requests": [S.advance_request_brief(r) for r in rows],
    })


@bp.route("/advance", methods=["POST"])
def advance_new():
    from app.services.advances import submit_advance_request, AdvanceError
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    body = _body()
    try:
        submit_advance_request(
            emp.company_id, emp.id,
            body.get("amount"),
            reason=body.get("reason"),
            created_by=current_user.id,
        )
        return jsonify({"ok": True}), 201
    except (AdvanceError, ValueError, TypeError, KeyError) as e:
        return _err(str(e), 400)


# ─── Attendance ───────────────────────────────────────────────────────
@bp.route("/attendance", methods=["GET"])
def attendance_month():
    """Read-only monthly view — matches hr_self_service.attendance."""
    from app.services.violation import (
        resolve_violation_policy_for_employee, approved_permissions_for,
    )
    from app.services.leave import exceptions_in_period
    from app.services.payroll import late_month_breakdown

    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()

    today = date.today()
    y, m = today.year, today.month
    month_start = date(y, m, 1)

    checkins = (AttendanceCheckin.query
                .filter_by(employee_id=emp.id)
                .filter(AttendanceCheckin.date >= month_start)
                .order_by(AttendanceCheckin.date.desc()).all())
    exceptions = exceptions_in_period(
        emp.company_id, y, m, employee_id=emp.id)
    permits = (LatePermissionRequest.query
               .filter_by(employee_id=emp.id)
               .filter(LatePermissionRequest.request_date >= month_start)
               .order_by(LatePermissionRequest.request_date.desc()).all())
    policy = resolve_violation_policy_for_employee(emp.id)
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

    return jsonify({
        "employee": S.employee_brief(emp),
        "month": f"{y}-{m:02d}",
        "checkins": [S.checkin_brief(c) for c in checkins],
        "exceptions": [
            {"id": e.id, "date": S.iso(e.date),
             "type": S.enum_of(e.type),
             "duration_hours": S.num(getattr(e, "duration_hours", None))}
            for e in exceptions
        ],
        "permits": [S.permission_request_brief(p) for p in permits],
        "policy_present": policy is not None,
        "remaining_late_pool_min": remaining_pool,
        "used_permits_this_month": used_permits,
        "remaining_permits_this_month": remaining_perms,
        "balances": [S.leave_balance_brief(b) for b in balances],
    })


@bp.route("/attendance/checkin", methods=["POST"])
def attendance_checkin():
    from app.services.attendance import check_in, AttendanceError
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    body = _body()
    # Use `??` semantics, not `or` — a legitimate lat=0.0 (equator) or
    # lng=0.0 (Greenwich meridian) is falsy in Python and would be
    # silently dropped by `a or b`. Fall through to the second key only
    # when the first is truly missing/invalid (None).
    lat = _coord("check_lat", body)
    if lat is None:
        lat = _coord("lat", body)
    lng = _coord("check_lng", body)
    if lng is None:
        lng = _coord("lng", body)
    try:
        row, exc = check_in(emp, lat=lat, lng=lng)
        return jsonify({
            "ok": True,
            "checkin": S.checkin_brief(row),
            "late_recorded": exc is not None,
        }), 201
    except AttendanceError as e:
        return _err(str(e), 400)


@bp.route("/attendance/checkout", methods=["POST"])
def attendance_checkout():
    from app.services.attendance import check_out, AttendanceError
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    body = _body()
    # Same `??`-not-`or` guard as attendance_checkin above.
    lat = _coord("check_lat", body)
    if lat is None:
        lat = _coord("lat", body)
    lng = _coord("check_lng", body)
    if lng is None:
        lng = _coord("lng", body)
    try:
        row = check_out(emp, lat=lat, lng=lng)
        return jsonify({
            "ok": True,
            "checkin": S.checkin_brief(row),
        })
    except AttendanceError as e:
        return _err(str(e), 400)


# ─── Daily reports ────────────────────────────────────────────────────
@bp.route("/daily-reports", methods=["GET"])
def daily_reports_list():
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    rows = EmployeeDailyReport.query.filter_by(
        employee_id=emp.id,
    ).order_by(EmployeeDailyReport.report_date.desc()).all()
    return jsonify({
        "count": len(rows),
        "reports": [S.daily_report_brief(r) for r in rows],
    })


@bp.route("/daily-reports/<int:report_id>", methods=["GET"])
def daily_report_detail(report_id):
    from app.services.daily_digest import build_digest
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    r = db.session.get(EmployeeDailyReport, report_id)
    if not r or r.employee_id != emp.id:
        return _err("not_found", 404)
    # Same DRAFT-refresh pattern as the web view.
    if r.status == DailyReportStatus.DRAFT:
        try:
            build_digest(r.company_id, r.employee_id, r.report_date)
            db.session.commit()
            db.session.refresh(r)
        except Exception:
            db.session.rollback()
    return jsonify({"report": S.daily_report_full(r)})


@bp.route("/daily-reports/<int:report_id>/notes", methods=["POST"])
def daily_report_notes(report_id):
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    r = db.session.get(EmployeeDailyReport, report_id)
    if not r or r.employee_id != emp.id:
        return _err("not_found", 404)
    if r.status == DailyReportStatus.SUBMITTED:
        return _err("already_submitted", 400)
    body = _body()
    r.employee_notes = (body.get("employee_notes") or "").strip() or None
    db.session.commit()
    return jsonify({"ok": True, "report": S.daily_report_full(r)})


@bp.route("/daily-reports/<int:report_id>/submit", methods=["POST"])
def daily_report_submit(report_id):
    from app.services.daily_digest import submit_report
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    r = db.session.get(EmployeeDailyReport, report_id)
    if not r or r.employee_id != emp.id:
        return _err("not_found", 404)
    if r.status == DailyReportStatus.SUBMITTED:
        return _err("already_submitted", 400)
    submit_report(r.id, current_user.id)
    db.session.commit()
    db.session.refresh(r)
    return jsonify({"ok": True, "report": S.daily_report_full(r)})


# ─── Cash custody ─────────────────────────────────────────────────────
@bp.route("/custody", methods=["GET"])
def custody_list():
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
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
    return jsonify({
        "custodies": [S.cash_custody_brief(c) for c in custodies],
        "requests": [S.cash_custody_request_brief(r) for r in requests_],
    })


@bp.route("/custody/<int:custody_id>", methods=["GET"])
def custody_detail(custody_id):
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    c = CashCustody.query.filter_by(
        id=custody_id, company_id=emp.company_id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
    ).first()
    if not c:
        return _err("not_found", 404)
    return jsonify({"custody": S.cash_custody_brief(c)})


@bp.route("/custody/request", methods=["POST"])
def custody_request_new():
    from app.services.cash_custody import request_custody, CustodyError
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    body = _body()
    try:
        due = _iso_date(body.get("needed_by_date"))
        request_custody(
            emp.company_id,
            CustodyHolderType.EMPLOYEE, emp.id,
            body.get("amount"),
            purpose=body.get("purpose"),
            needed_by_date=due,
            created_by=current_user.id,
        )
        return jsonify({"ok": True}), 201
    except (CustodyError, ValueError, TypeError, KeyError) as e:
        return _err(str(e), 400)


# ─── Item custody ─────────────────────────────────────────────────────
@bp.route("/items", methods=["GET"])
def items_list():
    from app.services.item_custody import items_available_for_company
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    mine = ItemCustody.query.filter_by(
        company_id=emp.company_id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
    ).order_by(ItemCustody.handed_over_on.desc()).limit(50).all()
    my_requests = ItemCustodyRequest.query.filter_by(
        company_id=emp.company_id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
    ).order_by(ItemCustodyRequest.created_at.desc()).limit(30).all()
    available = items_available_for_company(emp.company_id)
    return jsonify({
        "custodies": [S.item_custody_brief(c) for c in mine],
        "requests": [S.item_custody_request_brief(r) for r in my_requests],
        "available": [S.custody_item_brief(i) for i in available],
    })


@bp.route("/items/<int:custody_id>", methods=["GET"])
def items_detail(custody_id):
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    c = ItemCustody.query.filter_by(
        id=custody_id, company_id=emp.company_id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
    ).first()
    if not c:
        return _err("not_found", 404)
    return jsonify({"custody": S.item_custody_brief(c)})


@bp.route("/items/request", methods=["POST"])
def items_request_new():
    from app.services.item_custody import (
        request_item_custody, ItemCustodyError,
    )
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    body = _body()
    try:
        request_item_custody(
            emp.company_id,
            item_id=int(body.get("item_id") or 0),
            holder_type=CustodyHolderType.EMPLOYEE,
            holder_id=emp.id,
            purpose=body.get("purpose"),
            created_by=current_user.id,
        )
        return jsonify({"ok": True}), 201
    except (ItemCustodyError, ValueError, TypeError, KeyError) as e:
        return _err(str(e), 400)


# ─── My task archive ──────────────────────────────────────────────────
@bp.route("/archive", methods=["GET"])
def archive_list():
    from app.services.task_archive import my_archived_tasks
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    rows = my_archived_tasks(g.active_company.id, current_user.id).all()
    return jsonify({
        "count": len(rows),
        "tasks": [S.task_brief(t) for t in rows],
    })


@bp.route("/archive/<int:task_id>/restore", methods=["POST"])
def archive_restore(task_id):
    from app.services.task_archive import unarchive_task, can_restore_mine
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    t = db.session.get(Task, task_id)
    if (t is None
        or t.company_id != g.active_company.id
            or not can_restore_mine(t, current_user.id)):
        return _err("not_found", 404)
    unarchive_task(t, actor_id=current_user.id)
    return jsonify({"ok": True, "task": S.task_brief(t)})


# ─── My activity (90d, own only) ──────────────────────────────────────
@bp.route("/activity", methods=["GET"])
def activity():
    emp = _my_employee_or_404()
    if not emp:
        return _no_employee()
    hard_floor = datetime.utcnow() - timedelta(days=90)
    activities = (
        UserActivityLog.query
        .filter(UserActivityLog.user_id == current_user.id)
        .filter(UserActivityLog.company_id == g.active_company.id)
        .filter(UserActivityLog.created_at >= hard_floor)
        .order_by(UserActivityLog.created_at.desc())
        .limit(500).all()
    )
    sessions = (
        UserSession.query
        .filter(UserSession.user_id == current_user.id)
        .filter(UserSession.company_id == g.active_company.id)
        .filter(UserSession.login_at >= hard_floor)
        .order_by(UserSession.login_at.desc())
        .limit(200).all()
    )
    return jsonify({
        "activities": [
            {
                "id": a.id,
                "action_type": a.action_type,
                "entity_type": a.entity_type,
                "entity_id": a.entity_id,
                "entity_label": a.entity_label,
                "created_at": S.iso(a.created_at),
            }
            for a in activities
        ],
        "sessions": [
            {
                "id": s.id,
                "login_at": S.iso(s.login_at),
                "logout_at": S.iso(getattr(s, "logout_at", None)),
                "ip": getattr(s, "ip_address", None),
                "user_agent": getattr(s, "user_agent", None),
            }
            for s in sessions
        ],
    })
