"""MARSOUD-ITEM-CUSTODY-01 (2026-08-07) — item-custody blueprint
(/items).

Mirror of `app/routes/custody.py` structure. Accountant surface:
item registry (create + list + detail), requests inbox, settlement
form on each active custody, plus the "complete disposal" bridge
for fixed-asset-linked LOST/DAMAGED items.

Same permission gate as cash-custody per the ticket
("نفس صلاحية التذكرة الأولى — تُستخدم لإدارة النقدية والعينية معاً").
"""
from datetime import date, datetime

from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    Employee, EmployeeStatus, Department, FixedAsset,
    CustodyItem, ItemCustodyRequest, ItemCustody,
    ItemCustodyStatus, ItemCustodyRequestStatus,
    CustodyHolderType, DisposalReason,
)
from app.services.item_custody import (
    create_item, request_item_custody, approve_item_request,
    reject_item_request, settle_item_custody,
    complete_disposal_for_custody,
    items_available_for_company, items_pending_disposal,
    pending_requests_for_company, ItemCustodyError,
)
from app.services.assets import AssetError
from app.services.ledger import LedgerError
from app.services.permissions import require_permission


bp = Blueprint("item_custody", __name__)


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _own(model, obj_id):
    """Cross-tenant guard."""
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


def _company_assets_for_linking():
    """Non-disposed fixed assets in this company — the pool for
    linking a CustodyItem to an existing asset."""
    return FixedAsset.query.filter_by(
        company_id=g.active_company.id, is_disposed=False,
    ).order_by(FixedAsset.name).all()


# ─── Item registry ──────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("custody.manage")
def index():
    q = CustodyItem.query.filter_by(company_id=g.active_company.id)
    active_filter = request.args.get("is_active", "all")
    if active_filter == "yes":
        q = q.filter_by(is_active=True)
    elif active_filter == "no":
        q = q.filter_by(is_active=False)
    items = q.order_by(CustodyItem.name).all()
    # For each item, resolve current active custody (if any) — one
    # query for the whole set to avoid N+1.
    active_map = {}
    active_rows = ItemCustody.query.filter(
        ItemCustody.company_id == g.active_company.id,
        ItemCustody.status == ItemCustodyStatus.ACTIVE,
    ).all()
    for c in active_rows:
        active_map[c.item_id] = c
    pending_count = ItemCustodyRequest.query.filter_by(
        company_id=g.active_company.id,
        status=ItemCustodyRequestStatus.PENDING).count()
    pending_disposal = len(items_pending_disposal(g.active_company.id))
    return render_template(
        "items/index.html",
        items=items, active_map=active_map,
        pending_count=pending_count,
        pending_disposal=pending_disposal,
        active_filter=active_filter,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("custody.manage")
def new():
    if request.method == "POST":
        try:
            create_item(
                g.active_company.id,
                name=request.form.get("name"),
                serial_number=request.form.get("serial_number"),
                category=request.form.get("category"),
                fixed_asset_id=request.form.get("fixed_asset_id") or None,
                estimated_value=request.form.get("estimated_value") or None,
                created_by=current_user.id,
            )
            flash("تم تسجيل العنصر", "success")
            return redirect(url_for("item_custody.index"))
        except (ItemCustodyError, ValueError, TypeError) as e:
            flash(str(e), "error")
    return render_template(
        "items/form.html",
        assets=_company_assets_for_linking(),
    )


@bp.route("/<int:item_id>")
@login_required
@require_permission("custody.manage")
def detail_item(item_id):
    item = _own(CustodyItem, item_id)
    if not item:
        flash("العنصر غير موجود", "error")
        return redirect(url_for("item_custody.index"))
    history = ItemCustody.query.filter_by(
        item_id=item.id).order_by(
            ItemCustody.created_at.desc()).all()
    return render_template(
        "items/item_detail.html",
        item=item, history=history,
        employees=_active_employees(),
        departments=_active_departments(),
        holder_types=CustodyHolderType,
    )


# ─── Custody row detail (for settlement / disposal) ────────────
@bp.route("/custody/<int:custody_id>")
@login_required
@require_permission("custody.manage")
def detail(custody_id):
    custody = _own(ItemCustody, custody_id)
    if not custody:
        flash("العهدة غير موجودة", "error")
        return redirect(url_for("item_custody.index"))
    return render_template(
        "items/custody_detail.html",
        custody=custody,
        employees=_active_employees(),
        departments=_active_departments(),
        holder_types=CustodyHolderType,
        outcomes=ItemCustodyStatus,
        disposal_reasons=DisposalReason,
        today=date.today().isoformat(),
    )


@bp.route("/custody/<int:custody_id>/settle", methods=["POST"])
@login_required
@require_permission("custody.manage")
def settle(custody_id):
    custody = _own(ItemCustody, custody_id)
    if not custody:
        flash("العهدة غير موجودة", "error")
        return redirect(url_for("item_custody.index"))
    try:
        outcome = request.form.get("outcome", "").strip()
        settle_item_custody(
            custody, outcome,
            settled_on=_parse_date(request.form.get("settled_on"))
                        or date.today(),
            condition_at_return=request.form.get("condition_at_return"),
            settlement_note=request.form.get("settlement_note"),
            damage_value=request.form.get("damage_value") or 0,
            charged_to_employee=request.form.get(
                "charged_to_employee") == "on",
            transfer_holder_type=request.form.get(
                "transfer_holder_type") or None,
            transfer_holder_id=request.form.get(
                "transfer_holder_id") or None,
            actor_id=current_user.id,
        )
        flash("تمت التسوية", "success")
    except (ItemCustodyError, ValueError, TypeError,
             LedgerError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("item_custody.detail",
                              custody_id=custody.id))


@bp.route("/custody/<int:custody_id>/complete_disposal",
           methods=["POST"])
@login_required
@require_permission("custody.manage")
def complete_disposal(custody_id):
    """Bridge to dispose_asset() — only reachable when
    disposal_pending_at is set (fixed-asset-linked LOST/DAMAGED)."""
    custody = _own(ItemCustody, custody_id)
    if not custody:
        flash("العهدة غير موجودة", "error")
        return redirect(url_for("item_custody.index"))
    try:
        complete_disposal_for_custody(
            custody,
            disposal_date=_parse_date(request.form.get("disposal_date"))
                            or date.today(),
            reason=request.form.get("reason") or "LOST",
            proceeds=request.form.get("proceeds") or 0,
            note=request.form.get("note"),
            funding=request.form.get("funding", "cash"),
            actor_id=current_user.id,
        )
        flash(
            f"تم شطب الأصل المرتبط بالعهدة — قيد #"
            f"{custody.disposal_asset_result_id}",
            "success")
    except (ItemCustodyError, AssetError, LedgerError,
             ValueError, TypeError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("item_custody.detail",
                              custody_id=custody.id))


# ─── Requests inbox ─────────────────────────────────────────────
@bp.route("/requests")
@login_required
@require_permission("custody.manage")
def requests_index():
    status_filter = request.args.get("status", "PENDING")
    status = None
    if status_filter and status_filter != "ALL":
        try:
            status = ItemCustodyRequestStatus[status_filter]
        except KeyError:
            status_filter = "ALL"
    q = ItemCustodyRequest.query.filter_by(
        company_id=g.active_company.id)
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(ItemCustodyRequest.created_at.desc()).all()
    return render_template(
        "items/requests.html",
        requests=rows, status_filter=status_filter,
        statuses=ItemCustodyRequestStatus,
    )


@bp.route("/requests/<int:req_id>/approve", methods=["POST"])
@login_required
@require_permission("custody.manage")
def request_approve(req_id):
    req = _own(ItemCustodyRequest, req_id)
    if not req:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("item_custody.requests_index"))
    try:
        approve_item_request(
            req, reviewer_id=current_user.id,
            handed_over_on=_parse_date(
                request.form.get("handed_over_on")) or date.today(),
            condition_at_handover=request.form.get(
                "condition_at_handover"),
            review_note=request.form.get("review_note"),
        )
        flash(f"تم اعتماد الطلب وتسليم العنصر", "success")
    except ItemCustodyError as e:
        flash(str(e), "error")
    return redirect(url_for("item_custody.requests_index"))


@bp.route("/requests/<int:req_id>/reject", methods=["POST"])
@login_required
@require_permission("custody.manage")
def request_reject(req_id):
    req = _own(ItemCustodyRequest, req_id)
    if not req:
        flash("الطلب غير موجود", "error")
        return redirect(url_for("item_custody.requests_index"))
    try:
        reject_item_request(req, reviewer_id=current_user.id,
                             review_note=request.form.get("review_note"))
        flash("تم رفض الطلب", "success")
    except ItemCustodyError as e:
        flash(str(e), "error")
    return redirect(url_for("item_custody.requests_index"))
