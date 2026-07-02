"""MARSOUD-MANUFACTURING-01 — BOMs + work-order flow.

Route map:
    /manufacturing/boms/                 → list
    /manufacturing/boms/new              → create
    /manufacturing/boms/<id>             → detail (+ edit lines)
    /manufacturing/work-orders/          → list
    /manufacturing/work-orders/new       → create (DRAFT)
    /manufacturing/work-orders/<id>      → detail
    /manufacturing/work-orders/<id>/start   → DRAFT → IN_PROGRESS
    /manufacturing/work-orders/<id>/complete → post + close (requires
                                                manufacturing.complete)
    /manufacturing/work-orders/<id>/cancel  → close as CANCELLED
    /manufacturing/reports/              → cost roll-up table
"""
from datetime import date
from decimal import Decimal
from flask import (
    Blueprint, render_template, redirect, url_for, request, flash, g,
)
from flask_login import login_required, current_user
from app import db
from app.models import (
    BillOfMaterial, BOMLine, WorkOrder, WorkOrderStatus,
    WorkOrderConsumption, ProductVariant, Warehouse,
)
from app.services.numbering import next_number
from app.services.permissions import require_permission
from app.services.ledger import LedgerError
from app.services.manufacturing import (
    post_work_order_completion, ManufacturingError,
)


bp = Blueprint("manufacturing", __name__)


# ─── BOMs ──────────────────────────────────────────────────────────────
@bp.route("/boms/")
@login_required
@require_permission("manufacturing.view")
def boms_index():
    cid = g.active_company.id
    boms = BillOfMaterial.query.filter_by(company_id=cid).order_by(
        BillOfMaterial.created_at.desc(),
    ).all()
    return render_template("manufacturing/boms_index.html", boms=boms)


@bp.route("/boms/new", methods=["GET", "POST"])
@login_required
@require_permission("manufacturing.manage")
def bom_new():
    cid = g.active_company.id
    variants = ProductVariant.query.filter_by(
        company_id=cid, is_active=True,
    ).all()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        variant_id = request.form.get("product_variant_id", type=int)
        if not name or not variant_id:
            flash("الاسم والمنتج التام مطلوبان", "error")
            return render_template("manufacturing/bom_form.html",
                                     bom=None, variants=variants)
        bom = BillOfMaterial(
            company_id=cid, name=name,
            product_variant_id=variant_id,
        )
        db.session.add(bom); db.session.flush()

        # Parse repeating rows: component_variant_id[i], qty_per_unit[i]
        for i in range(0, 30):
            cvid = request.form.get(f"line_{i}_variant_id", type=int)
            qty = request.form.get(f"line_{i}_qty", type=float)
            if not cvid or not qty or qty <= 0:
                continue
            db.session.add(BOMLine(
                bom_id=bom.id,
                component_variant_id=cvid,
                qty_per_unit=Decimal(str(qty)),
            ))
        db.session.commit()
        flash("تم إنشاء التركيبة (BOM)", "success")
        return redirect(url_for("manufacturing.bom_detail", bom_id=bom.id))
    return render_template("manufacturing/bom_form.html",
                             bom=None, variants=variants)


@bp.route("/boms/<int:bom_id>")
@login_required
@require_permission("manufacturing.view")
def bom_detail(bom_id):
    cid = g.active_company.id
    bom = db.session.get(BillOfMaterial, bom_id)
    if not bom or bom.company_id != cid:
        flash("تركيبة غير موجودة", "error")
        return redirect(url_for("manufacturing.boms_index"))
    return render_template("manufacturing/bom_detail.html", bom=bom)


# ─── Work orders ───────────────────────────────────────────────────────
@bp.route("/work-orders/")
@login_required
@require_permission("manufacturing.view")
def work_orders_index():
    cid = g.active_company.id
    orders = WorkOrder.query.filter_by(company_id=cid).order_by(
        WorkOrder.created_at.desc(),
    ).all()
    return render_template("manufacturing/work_orders_index.html",
                             orders=orders)


@bp.route("/work-orders/new", methods=["GET", "POST"])
@login_required
@require_permission("manufacturing.manage")
def work_order_new():
    cid = g.active_company.id
    boms = BillOfMaterial.query.filter_by(
        company_id=cid, is_active=True,
    ).all()
    warehouses = Warehouse.query.filter_by(company_id=cid).all()
    if request.method == "POST":
        bom_id = request.form.get("bom_id", type=int)
        warehouse_id = request.form.get("warehouse_id", type=int)
        qty = request.form.get("quantity_to_produce", type=float)
        labor = request.form.get("direct_labor_cost", type=float) or 0.0
        overhead = request.form.get("overhead_cost", type=float) or 0.0
        if not (bom_id and warehouse_id and qty and qty > 0):
            flash("كل الحقول الأساسية مطلوبة", "error")
            return render_template("manufacturing/work_order_form.html",
                                     boms=boms, warehouses=warehouses)
        number = next_number(cid, "MANUFACTURING_ORDER")
        wo = WorkOrder(
            company_id=cid, number=number, bom_id=bom_id,
            warehouse_id=warehouse_id,
            quantity_to_produce=Decimal(str(qty)),
            direct_labor_cost=Decimal(str(labor)),
            overhead_cost=Decimal(str(overhead)),
            status=WorkOrderStatus.DRAFT,
            created_by=current_user.id,
        )
        db.session.add(wo); db.session.commit()
        flash(f"تم إنشاء أمر الإنتاج {number}", "success")
        return redirect(url_for("manufacturing.work_order_detail",
                                  work_order_id=wo.id))
    return render_template("manufacturing/work_order_form.html",
                             boms=boms, warehouses=warehouses)


@bp.route("/work-orders/<int:work_order_id>")
@login_required
@require_permission("manufacturing.view")
def work_order_detail(work_order_id):
    cid = g.active_company.id
    wo = db.session.get(WorkOrder, work_order_id)
    if not wo or wo.company_id != cid:
        flash("أمر إنتاج غير موجود", "error")
        return redirect(url_for("manufacturing.work_orders_index"))
    return render_template("manufacturing/work_order_detail.html", wo=wo)


@bp.route("/work-orders/<int:work_order_id>/start", methods=["POST"])
@login_required
@require_permission("manufacturing.manage")
def work_order_start(work_order_id):
    cid = g.active_company.id
    wo = db.session.get(WorkOrder, work_order_id)
    if not wo or wo.company_id != cid:
        return redirect(url_for("manufacturing.work_orders_index"))
    if wo.status != WorkOrderStatus.DRAFT:
        flash("الأمر ليس مسودة", "error")
    else:
        wo.status = WorkOrderStatus.IN_PROGRESS
        db.session.commit()
        flash("تم بدء الإنتاج", "success")
    return redirect(url_for("manufacturing.work_order_detail",
                              work_order_id=wo.id))


@bp.route("/work-orders/<int:work_order_id>/complete", methods=["POST"])
@login_required
@require_permission("manufacturing.complete")
def work_order_complete(work_order_id):
    cid = g.active_company.id
    wo = db.session.get(WorkOrder, work_order_id)
    if not wo or wo.company_id != cid:
        return redirect(url_for("manufacturing.work_orders_index"))
    try:
        post_work_order_completion(wo, created_by=current_user.id)
        flash(f"تم إكمال أمر الإنتاج {wo.number} وترحيل القيد.", "success")
    except (ManufacturingError, LedgerError) as e:
        flash(str(e), "error")
    return redirect(url_for("manufacturing.work_order_detail",
                              work_order_id=wo.id))


@bp.route("/work-orders/<int:work_order_id>/cancel", methods=["POST"])
@login_required
@require_permission("manufacturing.manage")
def work_order_cancel(work_order_id):
    cid = g.active_company.id
    wo = db.session.get(WorkOrder, work_order_id)
    if not wo or wo.company_id != cid:
        return redirect(url_for("manufacturing.work_orders_index"))
    if wo.status not in (WorkOrderStatus.DRAFT,
                          WorkOrderStatus.IN_PROGRESS):
        flash("لا يمكن إلغاء أمر مكتمل", "error")
    else:
        wo.status = WorkOrderStatus.CANCELLED
        db.session.commit()
        flash("تم إلغاء أمر الإنتاج", "success")
    return redirect(url_for("manufacturing.work_order_detail",
                              work_order_id=wo.id))


# ─── Reports ───────────────────────────────────────────────────────────
@bp.route("/reports/")
@login_required
@require_permission("manufacturing.view")
def reports():
    cid = g.active_company.id
    orders = WorkOrder.query.filter_by(
        company_id=cid, status=WorkOrderStatus.COMPLETED,
    ).order_by(WorkOrder.completed_at.desc()).all()
    rows = []
    for wo in orders:
        material = sum(c.total_cost for c in wo.consumption)
        labor = float(wo.direct_labor_cost or 0)
        overhead = float(wo.overhead_cost or 0)
        total = material + labor + overhead
        rows.append({
            "wo": wo,
            "material": material, "labor": labor,
            "overhead": overhead, "total": total,
        })
    return render_template("manufacturing/reports.html", rows=rows)
