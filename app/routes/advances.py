"""MARSOUD-ADVANCES — employee advances blueprint (/advances).

Three surfaces for the accountant/owner side:
  · /advances            — every advance with its balance, cancel button
  · /advances/new        — add one directly (no request stage)
  · /advances/requests   — employee requests waiting on a decision

The employee side lives on the portal (/my/account#advances) in
routes/hr_self_service.py.

Every route is gated on advances.manage (owner / admin / accountant) —
approving an advance disburses cash and posts a journal.
"""
from datetime import date, datetime

from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    Employee, EmployeeStatus, PaymentMethod,
    EmployeeAdvance, AdvanceRequest,
    AdvanceStatus, AdvanceSource, AdvanceRequestStatus,
)
from app.services.advances import (
    approve_advance, approve_advance_request, reject_advance_request,
    cancel_advance, advances_for_company, AdvanceError,
)
from app.services.permissions import require_permission


bp = Blueprint("advances", __name__)


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _payment_methods():
    return PaymentMethod.query.filter_by(
        company_id=g.active_company.id, is_active=True,
    ).order_by(PaymentMethod.is_default.desc(), PaymentMethod.name).all()


def _own(model, obj_id):
    """Load a row and refuse anything from another tenant."""
    row = db.session.get(model, obj_id)
    if not row or row.company_id != g.active_company.id:
        return None
    return row


# ─── All advances ───────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("advances.manage")
def index():
    status_filter = request.args.get("status", "ALL")
    status = None
    if status_filter and status_filter != "ALL":
        try:
            status = AdvanceStatus[status_filter]
        except KeyError:
            status_filter = "ALL"
    rows = advances_for_company(g.active_company.id, status=status)
    pending_count = AdvanceRequest.query.filter_by(
        company_id=g.active_company.id,
        status=AdvanceRequestStatus.PENDING,
    ).count()
    return render_template(
        "advances/index.html",
        advances=rows, status_filter=status_filter,
        statuses=AdvanceStatus, sources=AdvanceSource,
        pending_count=pending_count,
    )


# ─── Direct add ─────────────────────────────────────────────────────────
@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("advances.manage")
def new():
    cid = g.active_company.id
    employees = Employee.query.filter_by(
        company_id=cid, status=EmployeeStatus.ACTIVE,
    ).order_by(Employee.name).all()

    if request.method == "POST":
        try:
            emp_id = int(request.form.get("employee_id") or 0)
            adv = approve_advance(
                cid, emp_id,
                request.form.get("amount"),
                request.form.get("months") or 1,
                _parse_date(request.form.get("disbursed_on")) or date.today(),
                source=AdvanceSource.DIRECT,
                actor_id=current_user.id,
                payment_method_id=request.form.get("payment_method_id") or None,
                note=request.form.get("note"),
            )
            flash(
                f"تم صرف سلفة {float(adv.amount):.2f} لـ {adv.employee.name} "
                f"— القسط الشهري {float(adv.monthly_installment):.2f}",
                "success",
            )
            return redirect(url_for("advances.index"))
        except (AdvanceError, ValueError, TypeError) as e:
            flash(str(e), "error")

    return render_template(
        "advances/form.html",
        employees=employees, payment_methods=_payment_methods(),
        today=date.today().isoformat(),
    )


# ─── Employee requests ──────────────────────────────────────────────────
@bp.route("/requests")
@login_required
@require_permission("advances.manage")
def requests():
    status_filter = request.args.get("status", "PENDING")
    q = AdvanceRequest.query.filter_by(company_id=g.active_company.id)
    if status_filter and status_filter != "ALL":
        try:
            q = q.filter_by(status=AdvanceRequestStatus[status_filter])
        except KeyError:
            status_filter = "ALL"
    rows = q.order_by(AdvanceRequest.created_at.desc()).limit(200).all()
    return render_template(
        "advances/requests.html",
        requests=rows, status_filter=status_filter,
        statuses=AdvanceRequestStatus,
    )


@bp.route("/requests/<int:req_id>/approve", methods=["GET", "POST"])
@login_required
@require_permission("advances.manage")
def request_approve(req_id):
    req = _own(AdvanceRequest, req_id)
    if not req:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("advances.requests"))

    if request.method == "POST":
        try:
            adv = approve_advance_request(
                req, reviewer_id=current_user.id,
                months=request.form.get("months") or 1,
                disbursed_on=(_parse_date(request.form.get("disbursed_on"))
                              or date.today()),
                payment_method_id=request.form.get("payment_method_id") or None,
                review_note=request.form.get("review_note"),
            )
            flash(
                f"تم اعتماد السلفة وصرفها — القسط الشهري "
                f"{float(adv.monthly_installment):.2f}",
                "success",
            )
            return redirect(url_for("advances.requests"))
        except (AdvanceError, ValueError, TypeError) as e:
            flash(str(e), "error")

    return render_template(
        "advances/approve_form.html",
        req=req, payment_methods=_payment_methods(),
        today=date.today().isoformat(),
    )


@bp.route("/requests/<int:req_id>/reject", methods=["POST"])
@login_required
@require_permission("advances.manage")
def request_reject(req_id):
    req = _own(AdvanceRequest, req_id)
    if not req:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("advances.requests"))
    try:
        reject_advance_request(req, reviewer_id=current_user.id,
                               review_note=request.form.get("review_note"))
        flash("تم رفض الطلب", "success")
    except AdvanceError as e:
        flash(str(e), "error")
    return redirect(url_for("advances.requests"))


# ─── Cancel ─────────────────────────────────────────────────────────────
@bp.route("/<int:advance_id>/cancel", methods=["POST"])
@login_required
@require_permission("advances.manage")
def cancel(advance_id):
    adv = _own(EmployeeAdvance, advance_id)
    if not adv:
        flash("السلفة غير موجودة", "error")
        return redirect(url_for("advances.index"))
    try:
        cancel_advance(adv, actor_id=current_user.id,
                       reason=request.form.get("reason"))
        flash("تم إلغاء السلفة وعكس قيد الصرف — لن تُخصم في الرواتب القادمة",
              "success")
    except AdvanceError as e:
        flash(str(e), "error")
    return redirect(url_for("advances.index"))
