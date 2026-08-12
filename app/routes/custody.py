"""MARSOUD-CASH-CUSTODY-01 (2026-08-07) — cash-custody blueprint
(/custody).

Six surfaces for the accountant/owner side:
  · /custody                — every custody with status + settle status
  · /custody/new            — issue one directly (accountant path)
  · /custody/requests       — employee requests waiting on a decision
  · /custody/requests/<id>/approve  — approve + issue in one flow
  · /custody/requests/<id>/reject   — reject with a note
  · /custody/<id>           — detail: add settlement lines, close, cancel

The portal side (employee submits their own request, uploads a
receipt) lives at /my/custody in routes/hr_self_service.py — added
in the next slice.

Every route is gated on custody.manage (owner / admin / accountant)
except the read-only detail-view helpers a settlement-viewer might
need later. Approving / issuing / settling / cancelling moves cash
and posts a journal.
"""
from datetime import date, datetime

from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    Employee, EmployeeStatus, Department, PaymentMethod, Account,
    CashCustody, CashCustodyRequest,
    CustodyStatus, CustodyRequestStatus, CustodyHolderType,
    ShortfallDisposition,
)
from app.services.cash_custody import (
    request_custody, approve_custody_request, reject_custody_request,
    delete_pending_request, reopen_settlement,
    issue_custody, add_settlement_line, close_settlement,
    cancel_custody, custodies_for_company,
    requests_for_company, CustodyError,
)
from app.services.permissions import require_permission


bp = Blueprint("custody", __name__)


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
    """Load a row and refuse anything from another tenant. Mirrors
    the guard used by advances._own — cross-tenant guard runs BEFORE
    the row's ORM relationships are touched, so a crafted URL that
    walked the FKs from company B can't leak."""
    row = db.session.get(model, obj_id)
    if not row or row.company_id != g.active_company.id:
        return None
    return row


def _active_employees():
    return Employee.query.filter_by(
        company_id=g.active_company.id, status=EmployeeStatus.ACTIVE,
    ).order_by(Employee.name).all()


def _active_departments():
    return Department.query.filter_by(
        company_id=g.active_company.id, is_active=True,
    ).order_by(Department.name).all()


def _postable_expense_accounts():
    """Every postable EXPENSE account for the company — used to
    populate the 'expense_account_id' dropdown when adding a
    settlement line. Filtering to postable prevents the
    add_settlement_line service raising on a header account."""
    from app.models import AccountType
    return Account.query.filter_by(
        company_id=g.active_company.id, is_postable=True,
        type=AccountType.EXPENSE,
    ).order_by(Account.code).all()


# ─── Index ──────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("custody.manage")
def index():
    status_filter = request.args.get("status", "ALL")
    status = None
    if status_filter and status_filter != "ALL":
        try:
            status = CustodyStatus[status_filter]
        except KeyError:
            status_filter = "ALL"
    rows = custodies_for_company(g.active_company.id, status=status)
    pending_count = CashCustodyRequest.query.filter_by(
        company_id=g.active_company.id,
        status=CustodyRequestStatus.PENDING,
    ).count()
    return render_template(
        "custody/index.html",
        custodies=rows, status_filter=status_filter,
        statuses=CustodyStatus,
        pending_count=pending_count,
    )


# ─── Direct issue ───────────────────────────────────────────────
@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("custody.manage")
def new():
    cid = g.active_company.id
    if request.method == "POST":
        try:
            holder_type_raw = request.form.get("holder_type") or "EMPLOYEE"
            holder_type = CustodyHolderType(holder_type_raw.upper())
            holder_id = int(request.form.get(
                "employee_id" if holder_type == CustodyHolderType.EMPLOYEE
                else "department_id") or 0)
            custody = issue_custody(
                cid,
                holder_type=holder_type,
                holder_id=holder_id,
                amount=request.form.get("amount"),
                purpose=request.form.get("purpose"),
                issued_on=(_parse_date(request.form.get("issued_on"))
                            or date.today()),
                settlement_due_date=_parse_date(
                    request.form.get("settlement_due_date")),
                payment_method_id=request.form.get(
                    "payment_method_id") or None,
                actor_id=current_user.id,
                note=request.form.get("note"),
            )
            flash(
                f"تم صرف عهدة {float(custody.amount_issued):.2f} "
                f"لـ {custody.holder_name}",
                "success",
            )
            return redirect(url_for("custody.detail",
                                    custody_id=custody.id))
        except (CustodyError, ValueError, TypeError) as e:
            flash(str(e), "error")

    return render_template(
        "custody/form.html",
        employees=_active_employees(),
        departments=_active_departments(),
        payment_methods=_payment_methods(),
        today=date.today().isoformat(),
        holder_types=CustodyHolderType,
    )


# ─── Requests ───────────────────────────────────────────────────
@bp.route("/requests")
@login_required
@require_permission("custody.manage")
def requests():
    status_filter = request.args.get("status", "PENDING")
    status = None
    if status_filter and status_filter != "ALL":
        try:
            status = CustodyRequestStatus[status_filter]
        except KeyError:
            status_filter = "ALL"
    rows = requests_for_company(g.active_company.id, status=status)
    return render_template(
        "custody/requests.html",
        requests=rows, status_filter=status_filter,
        statuses=CustodyRequestStatus,
    )


@bp.route("/requests/<int:req_id>/approve", methods=["GET", "POST"])
@login_required
@require_permission("custody.manage")
def request_approve(req_id):
    req = _own(CashCustodyRequest, req_id)
    if not req:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("custody.requests"))

    if request.method == "POST":
        try:
            # MARSOUD-CUSTODY-REQUEST-APPROVE-01 (2026-08-10) —
            # optional amount override. Empty string / missing →
            # None → service uses req.amount (old behaviour).
            amount_raw = (request.form.get("amount") or "").strip()
            amount = amount_raw or None
            custody = approve_custody_request(
                req, reviewer_id=current_user.id,
                issued_on=(_parse_date(request.form.get("issued_on"))
                            or date.today()),
                settlement_due_date=_parse_date(
                    request.form.get("settlement_due_date")),
                payment_method_id=(
                    request.form.get("payment_method_id") or None),
                review_note=request.form.get("review_note"),
                amount=amount,
            )
            flash(
                f"تم اعتماد الطلب وصرف العهدة — "
                f"{float(custody.amount_issued):.2f} "
                f"لـ {custody.holder_name}",
                "success",
            )
            # MARSOUD-CUSTODY-REQUEST-APPROVE-01 (2026-08-10) —
            # transfer-receipt upload rides in the same POST
            # (approve_form.html carries enctype=multipart). We
            # save AFTER the approve commits so a bad file
            # (wrong extension, too big) doesn't roll back a
            # money-moving operation — the accountant can retry
            # the upload from the request list. Flash a warning
            # on file failure but leave the approval standing.
            file_storage = request.files.get("receipt")
            if file_storage and file_storage.filename:
                try:
                    from app.services.opsflow_extras import (
                        save_document,
                    )
                    save_document(
                        company_id=req.company_id,
                        source_type="CASH_CUSTODY_REQUEST",
                        source_id=req.id,
                        file_storage=file_storage,
                        uploaded_by_id=current_user.id,
                    )
                except Exception as e:  # noqa: BLE001
                    flash(
                        f"⚠ تم الاعتماد، لكن رفع الإيصال فشل: {e}",
                        "warning",
                    )
            return redirect(url_for("custody.detail",
                                    custody_id=custody.id))
        except (CustodyError, ValueError, TypeError) as e:
            flash(str(e), "error")

    return render_template(
        "custody/approve_form.html",
        req=req, payment_methods=_payment_methods(),
        today=date.today().isoformat(),
    )


@bp.route("/requests/<int:req_id>/reject", methods=["POST"])
@login_required
@require_permission("custody.manage")
def request_reject(req_id):
    req = _own(CashCustodyRequest, req_id)
    if not req:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("custody.requests"))
    try:
        reject_custody_request(req, reviewer_id=current_user.id,
                                review_note=request.form.get("review_note"))
        flash("تم رفض الطلب", "success")
    except CustodyError as e:
        flash(str(e), "error")
    return redirect(url_for("custody.requests"))


# ─── Detail + settlement ────────────────────────────────────────
@bp.route("/<int:custody_id>")
@login_required
@require_permission("custody.manage")
def detail(custody_id):
    custody = _own(CashCustody, custody_id)
    if not custody:
        flash("العهدة غير موجودة", "error")
        return redirect(url_for("custody.index"))
    return render_template(
        "custody/detail.html",
        custody=custody,
        expense_accounts=_postable_expense_accounts(),
        shortfall_options=ShortfallDisposition,
        today=date.today().isoformat(),
    )


@bp.route("/<int:custody_id>/settle", methods=["POST"])
@login_required
@require_permission("custody.manage")
def add_line(custody_id):
    custody = _own(CashCustody, custody_id)
    if not custody:
        flash("العهدة غير موجودة", "error")
        return redirect(url_for("custody.index"))
    try:
        add_settlement_line(
            custody,
            expense_account_id=int(
                request.form.get("expense_account_id") or 0),
            amount=request.form.get("amount"),
            receipt_note=request.form.get("receipt_note"),
            actor_id=current_user.id,
        )
        flash("تمت إضافة بند التسوية", "success")
    except (CustodyError, ValueError, TypeError) as e:
        flash(str(e), "error")
    return redirect(url_for("custody.detail", custody_id=custody.id))


@bp.route("/<int:custody_id>/close", methods=["POST"])
@login_required
@require_permission("custody.manage")
def close(custody_id):
    custody = _own(CashCustody, custody_id)
    if not custody:
        flash("العهدة غير موجودة", "error")
        return redirect(url_for("custody.index"))
    try:
        close_settlement(
            custody, actor_id=current_user.id,
            returned_amount=request.form.get("returned_amount") or 0,
            shortfall_disposition=(
                request.form.get("shortfall_disposition") or None),
            settlement_date=(
                _parse_date(request.form.get("settlement_date"))
                or date.today()),
        )
        flash("تم إقفال التسوية وترحيل قيد الإقفال", "success")
    except (CustodyError, ValueError, TypeError) as e:
        flash(str(e), "error")
    return redirect(url_for("custody.detail", custody_id=custody.id))


@bp.route("/<int:custody_id>/cancel", methods=["POST"])
@login_required
@require_permission("custody.manage")
def cancel(custody_id):
    custody = _own(CashCustody, custody_id)
    if not custody:
        flash("العهدة غير موجودة", "error")
        return redirect(url_for("custody.index"))
    try:
        cancel_custody(custody, actor_id=current_user.id,
                        reason=request.form.get("reason"))
        flash("تم إلغاء العهدة وعكس قيد الصرف", "success")
    except CustodyError as e:
        flash(str(e), "error")
    return redirect(url_for("custody.index"))


# ─── MARSOUD-CUSTODY-DELETE-CONSISTENCY (2026-08-12) ─────────────
# Three new endpoints. Delete-pending-request (AC #1) enables safe
# hard-delete of a request that has no journal yet. Delete-custody
# always refuses (AC #2 defense-in-depth at the route layer, even
# though no UI button offers it). Reopen (AC #4) undoes a
# close_settlement by reversing the settlement JE.
@bp.route("/requests/<int:req_id>/delete", methods=["POST"])
@login_required
@require_permission("custody.manage")
def request_delete(req_id):
    """AC #1 — hard-delete a PENDING request. Any other status
    refuses (use reject / cancel instead)."""
    req = _own(CashCustodyRequest, req_id)
    if not req:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("custody.requests"))
    try:
        delete_pending_request(req, actor_id=current_user.id)
        flash("تم حذف الطلب", "success")
    except CustodyError as e:
        flash(str(e), "error")
    return redirect(url_for("custody.requests"))


@bp.route("/<int:custody_id>/delete", methods=["POST"])
@login_required
@require_permission("custody.manage")
def custody_delete_refused(custody_id):
    """AC #2 — any attempted delete on an issued custody is
    refused; the accountant is redirected to /cancel instead.
    No UI offers this — the endpoint exists so a crafted POST
    or future UI mistake can't accidentally hard-delete a row
    with a live journal entry."""
    custody = _own(CashCustody, custody_id)
    if not custody:
        flash("العهدة غير موجودة", "error")
        return redirect(url_for("custody.index"))
    flash(
        "لا يمكن حذف عهدة صادرة — أي حركة مالية اتم صرفها فعلاً "
        "لازم تتعكس بقيد يومية، مش تتمسح. "
        "استخدم زر 'إلغاء العهدة' من صفحة التفاصيل بدل الحذف.",
        "error",
    )
    return redirect(url_for("custody.detail",
                             custody_id=custody.id))


@bp.route("/<int:custody_id>/reopen", methods=["POST"])
@login_required
@require_permission("custody.manage")
def reopen(custody_id):
    """AC #4 — undo a close_settlement. Reverses the settlement
    JE, flips custody back to PARTIALLY_SETTLED / ISSUED.
    Original issue JE stays untouched."""
    custody = _own(CashCustody, custody_id)
    if not custody:
        flash("العهدة غير موجودة", "error")
        return redirect(url_for("custody.index"))
    try:
        reopen_settlement(
            custody, actor_id=current_user.id,
            reason=request.form.get("reason"),
        )
        flash("تم التراجع عن التسوية — القيد العكسي مُرحّل",
              "success")
    except CustodyError as e:
        flash(str(e), "error")
    return redirect(url_for("custody.detail",
                             custody_id=custody.id))
