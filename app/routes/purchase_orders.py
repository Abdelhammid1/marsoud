"""MARSOUD-PURCHASE-ORDERS-01 (2026-09-02) — routes.

Blueprint `purchase_orders` mounted at `/purchase-orders`. Nine
endpoints (index, new, create, detail, approve, reject, cancel,
receive, delete, pending_report). Every state-mutating call routes
through `app/services/purchase_orders.py` so counter bumps + status
flips + audit logging land in one place.
"""
from datetime import datetime
from flask import (
    Blueprint, render_template, redirect, url_for, request, g, flash,
    abort,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    PurchaseOrder, PurchaseOrderItem, PurchaseOrderStatus,
    Vendor,
)
from app.services.permissions import require_permission, has_permission
from app.services.purchase_orders import (
    PurchaseOrderError, create_po, approve_po, reject_po, cancel_po,
    delete_po, receive_purchase_order_items, pending_pos_report,
)


bp = Blueprint("purchase_orders", __name__)


def _load_po_or_404(po_id):
    po = db.session.get(PurchaseOrder, int(po_id))
    if not po or po.company_id != g.active_company.id:
        abort(404)
    if po.deleted_at is not None:
        # Same shape as vendor_bills — treat soft-deleted as gone.
        abort(404)
    return po


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


# ─── Index ──────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("purchase_orders.view")
def index():
    cid = g.active_company.id
    q = (PurchaseOrder.query
         .filter_by(company_id=cid)
         .filter(PurchaseOrder.deleted_at.is_(None)))
    status = (request.args.get("status") or "").strip()
    vendor_id = request.args.get("vendor_id", type=int)
    if status == "pending":
        q = q.filter(PurchaseOrder.status.in_((
            PurchaseOrderStatus.REQUESTED,
            PurchaseOrderStatus.APPROVED,
            PurchaseOrderStatus.PARTIALLY_RECEIVED,
        )))
    elif status:
        try:
            q = q.filter(PurchaseOrder.status
                          == PurchaseOrderStatus(status))
        except ValueError:
            pass
    if vendor_id:
        q = q.filter(PurchaseOrder.vendor_id == vendor_id)
    rows = q.order_by(PurchaseOrder.issue_date.desc()).all()
    return render_template(
        "purchase_orders/index.html",
        rows=rows, filters={"status": status, "vendor_id": vendor_id},
        vendors=Vendor.query.filter_by(company_id=cid)
                .order_by(Vendor.name).all(),
        statuses=PurchaseOrderStatus,
    )


# ─── New (GET form) ─────────────────────────────────────────────
@bp.route("/new")
@login_required
@require_permission("purchase_orders.request")
def new():
    cid = g.active_company.id
    return render_template(
        "purchase_orders/new.html",
        vendors=Vendor.query.filter_by(company_id=cid)
                .order_by(Vendor.name).all(),
    )


# ─── Create ─────────────────────────────────────────────────────
@bp.route("/", methods=["POST"])
@login_required
@require_permission("purchase_orders.request")
def create():
    cid = g.active_company.id
    # Item rows arrive as indexed lists: description[], quantity[], …
    descs = request.form.getlist("description[]")
    qtys = request.form.getlist("quantity[]")
    prices = request.form.getlist("unit_price[]")
    items = []
    for i, desc in enumerate(descs):
        if not (desc or "").strip():
            continue
        items.append({
            "description": desc,
            "quantity": qtys[i] if i < len(qtys) else 0,
            "unit_price": prices[i] if i < len(prices) else 0,
            "line_type": "INVENTORY",
        })
    try:
        po = create_po(
            cid,
            vendor_id=request.form.get("vendor_id", type=int),
            items=items,
            currency=g.active_company.base_currency or "SAR",
            issue_date=_parse_date(request.form.get("issue_date")),
            expected_date=_parse_date(request.form.get("expected_date")),
            tax_rate=request.form.get("tax_rate") or 0,
            notes=request.form.get("notes"),
            requested_by_id=current_user.id,
        )
        flash("تم إنشاء طلب الشراء", "success")
        return redirect(url_for("purchase_orders.detail", po_id=po.id))
    except PurchaseOrderError as e:
        flash(str(e), "error")
        return redirect(url_for("purchase_orders.new"))


# ─── Detail ─────────────────────────────────────────────────────
@bp.route("/<int:po_id>")
@login_required
@require_permission("purchase_orders.view")
def detail(po_id):
    po = _load_po_or_404(po_id)
    can_bill = (
        has_permission("purchase_orders.convert_to_bill")
        and po.status in (PurchaseOrderStatus.RECEIVED,
                           PurchaseOrderStatus.PARTIALLY_RECEIVED)
        and any(i.qty_remaining_to_invoice > 0 for i in po.items)
    )
    return render_template(
        "purchase_orders/detail.html",
        po=po, can_bill=can_bill,
    )


# ─── Approve ────────────────────────────────────────────────────
@bp.route("/<int:po_id>/approve", methods=["POST"])
@login_required
@require_permission("purchase_orders.approve")
def approve(po_id):
    po = _load_po_or_404(po_id)
    try:
        approve_po(po, actor_id=current_user.id)
        flash("تم اعتماد أمر الشراء", "success")
    except PurchaseOrderError as e:
        flash(str(e), "error")
    return redirect(url_for("purchase_orders.detail", po_id=po.id))


# ─── Reject ─────────────────────────────────────────────────────
@bp.route("/<int:po_id>/reject", methods=["POST"])
@login_required
@require_permission("purchase_orders.approve")
def reject(po_id):
    po = _load_po_or_404(po_id)
    try:
        reject_po(po, reason=request.form.get("reason"),
                   actor_id=current_user.id)
        flash("تم رفض أمر الشراء", "success")
    except PurchaseOrderError as e:
        flash(str(e), "error")
    return redirect(url_for("purchase_orders.detail", po_id=po.id))


# ─── Cancel ─────────────────────────────────────────────────────
@bp.route("/<int:po_id>/cancel", methods=["POST"])
@login_required
@require_permission("purchase_orders.cancel")
def cancel(po_id):
    po = _load_po_or_404(po_id)
    try:
        cancel_po(po, reason=request.form.get("reason"),
                   actor_id=current_user.id)
        flash("تم إلغاء أمر الشراء", "success")
    except PurchaseOrderError as e:
        flash(str(e), "error")
    return redirect(url_for("purchase_orders.detail", po_id=po.id))


# ─── Receive (GRN) ──────────────────────────────────────────────
@bp.route("/<int:po_id>/receive", methods=["GET", "POST"])
@login_required
@require_permission("purchase_orders.receive")
def receive(po_id):
    po = _load_po_or_404(po_id)
    if request.method == "GET":
        return render_template("purchase_orders/receive.html", po=po)
    # POST
    po_item_ids = request.form.getlist("po_item_id[]")
    qtys = request.form.getlist("quantity_received[]")
    items_data = []
    for i, pid in enumerate(po_item_ids):
        try:
            items_data.append({
                "po_item_id": int(pid),
                "quantity_received": float(
                    qtys[i] if i < len(qtys) else 0),
            })
        except ValueError:
            continue
    try:
        grn = receive_purchase_order_items(
            po, items_data, received_by_id=current_user.id,
            notes=request.form.get("notes"),
            received_date=_parse_date(request.form.get("received_date")),
        )
        flash(f"تم تسجيل إذن الاستلام {grn.number}", "success")
    except PurchaseOrderError as e:
        flash(str(e), "error")
    return redirect(url_for("purchase_orders.detail", po_id=po.id))


# ─── Delete (soft, REQUESTED only) ──────────────────────────────
@bp.route("/<int:po_id>/delete", methods=["POST"])
@login_required
@require_permission("purchase_orders.request")
def delete(po_id):
    po = _load_po_or_404(po_id)
    try:
        delete_po(po, actor_id=current_user.id)
        flash("تم حذف طلب الشراء", "success")
        return redirect(url_for("purchase_orders.index"))
    except PurchaseOrderError as e:
        flash(str(e), "error")
        return redirect(url_for("purchase_orders.detail", po_id=po.id))


# ─── Pending report ─────────────────────────────────────────────
@bp.route("/pending-report")
@login_required
@require_permission("purchase_orders.view")
def pending_report():
    cid = g.active_company.id
    vendor_id = request.args.get("vendor_id", type=int)
    rows = pending_pos_report(cid, vendor_id=vendor_id)
    return render_template(
        "purchase_orders/pending_report.html",
        rows=rows,
        vendors=Vendor.query.filter_by(company_id=cid)
                .order_by(Vendor.name).all(),
        selected_vendor=vendor_id,
    )
