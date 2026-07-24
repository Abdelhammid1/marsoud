"""MARSOUD-DUAL-UOM-WEIGHT-01 UI (Abdelhamid 2026-07-24).

Physical stock count screen. Owner-only in practice via the
inventory.manage permission — same gate as manual adjustments,
which is what a count effectively is.
"""
from decimal import Decimal
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request,
    g, abort,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    ProductVariant, Warehouse, StockBalance, Product, InventoryCount,
    INV_COUNT_DRAFT, INV_COUNT_CONFIRMED,
)
from app.services.inventory import (
    start_inventory_count, commit_inventory_count, InventoryError,
)
from app.services.permissions import require_permission


bp = Blueprint("inventory_counts", __name__)


@bp.route("/")
@login_required
@require_permission("inventory.manage")
def index():
    cid = g.active_company.id
    rows = InventoryCount.query.filter_by(
        company_id=cid,
    ).order_by(InventoryCount.counted_at.desc()).limit(100).all()
    return render_template(
        "inventory/counts/index.html", rows=rows,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("inventory.manage")
def new():
    cid = g.active_company.id
    if request.method == "POST":
        variant_id = request.form.get("variant_id", type=int)
        warehouse_id = request.form.get("warehouse_id", type=int)
        try:
            counted_qty = Decimal(request.form.get("counted_qty") or "0")
        except Exception:
            counted_qty = Decimal("0")
        try:
            counted_pieces = Decimal(
                request.form.get("counted_pieces") or "0")
        except Exception:
            counted_pieces = Decimal("0")
        v = db.session.get(ProductVariant, variant_id)
        w = db.session.get(Warehouse, warehouse_id)
        if not v or not w or v.company_id != cid or w.company_id != cid:
            flash("الصنف أو المخزن غير صحيح", "error")
            return redirect(url_for("inventory_counts.new"))
        try:
            row = start_inventory_count(
                variant=v, warehouse=w,
                counted_qty=counted_qty,
                counted_pieces=counted_pieces,
                counted_by_id=current_user.id,
            )
        except InventoryError as e:
            flash(str(e), "error")
            return redirect(url_for("inventory_counts.new"))
        # Immediately show the DRAFT row + variance for the operator
        # to confirm.
        return redirect(url_for("inventory_counts.detail",
                                  count_id=row.id))
    # GET — form.
    variants = (db.session.query(ProductVariant, Product)
                .join(Product, Product.id == ProductVariant.product_id)
                .filter(Product.company_id == cid,
                        ProductVariant.is_active.is_(True),
                        Product.is_tracked.is_(True))
                .order_by(Product.name.asc()).all())
    warehouses = Warehouse.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(Warehouse.is_default.desc()).all()
    return render_template(
        "inventory/counts/new.html",
        variants=variants, warehouses=warehouses,
    )


@bp.route("/<int:count_id>")
@login_required
@require_permission("inventory.manage")
def detail(count_id):
    row = _owned_or_404(count_id)
    return render_template("inventory/counts/detail.html", row=row)


@bp.route("/<int:count_id>/confirm", methods=["POST"])
@login_required
@require_permission("inventory.manage")
def confirm(count_id):
    row = _owned_or_404(count_id)
    try:
        commit_inventory_count(row, actor_id=current_user.id)
        flash("تم تأكيد الجرد وترحيل تسوية الفرق", "success")
    except InventoryError as e:
        flash(str(e), "error")
    return redirect(url_for("inventory_counts.detail", count_id=row.id))


def _owned_or_404(count_id):
    row = db.session.get(InventoryCount, count_id)
    if not row or row.company_id != g.active_company.id:
        abort(404)
    return row
