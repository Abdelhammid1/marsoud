"""MARSOUD-ERP-01 — inventory routes.

Owner / admin / accountant / inventory_manager (the role lands in a
separate ticket). Phase 1 surface:

  /inventory/                    dashboard (totals, low-stock, recent)
  /inventory/warehouses/         list + add
  /inventory/movements/          unified audit log with filters
  /inventory/adjust              new manual count adjustment
  /inventory/opening-balance     bulk first-time entry
  /inventory/variants/<id>       variant detail page
  /inventory/products/<id>/stock per-warehouse balances for a product
  /inventory/movements/recheck   recompute cached balances; flag drift

POS, transfers, reports come in Phase 2/3.
"""
from datetime import datetime, date, timedelta
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g, abort,
    jsonify,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    Product, ProductVariant, Warehouse, StockBalance, StockMovement,
    StockMovementKind, User, Company,
)
from app.services.permissions import require_permission
from app.services.inventory import (
    record_adjustment, record_opening_balance, recompute_balance,
    low_stock_variants, total_inventory_value, default_warehouse,
    InventoryError,
)


bp = Blueprint("inventory", __name__)


# ─── Dashboard ───────────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("inventory.view")
def index():
    cid = g.active_company.id
    variants_count = ProductVariant.query.filter_by(
        company_id=cid, is_active=True).count()
    total_value = total_inventory_value(cid)
    low_stock = low_stock_variants(cid)
    recent = StockMovement.query.filter_by(company_id=cid).order_by(
        StockMovement.created_at.desc()).limit(15).all()
    return render_template(
        "inventory/index.html",
        variants_count=variants_count,
        total_value=total_value,
        low_stock=low_stock,
        recent=recent,
        kinds=StockMovementKind,
    )


# ─── Warehouses ──────────────────────────────────────────────────────────
@bp.route("/warehouses/")
@login_required
@require_permission("inventory.view")
def warehouses():
    cid = g.active_company.id
    items = Warehouse.query.filter_by(company_id=cid).order_by(
        Warehouse.is_default.desc(), Warehouse.code).all()
    return render_template("inventory/warehouses.html", warehouses=items)


@bp.route("/warehouses/new", methods=["POST"])
@login_required
@require_permission("inventory.manage")
def warehouse_new():
    cid = g.active_company.id
    code = (request.form.get("code") or "").strip().upper()
    name = (request.form.get("name") or "").strip()
    if not code or not name:
        flash("الكود والاسم مطلوبان", "error")
        return redirect(url_for("inventory.warehouses"))
    if Warehouse.query.filter_by(company_id=cid, code=code).first():
        flash("الكود مستخدم بالفعل", "error")
        return redirect(url_for("inventory.warehouses"))
    db.session.add(Warehouse(
        company_id=cid, code=code, name=name,
        is_default=False, is_active=True,
    ))
    db.session.commit()
    flash(f"تم إضافة المخزن {code}", "success")
    return redirect(url_for("inventory.warehouses"))


@bp.route("/warehouses/<int:wh_id>/toggle", methods=["POST"])
@login_required
@require_permission("inventory.manage")
def warehouse_toggle(wh_id):
    cid = g.active_company.id
    wh = db.session.get(Warehouse, wh_id)
    if not wh or wh.company_id != cid:
        abort(404)
    if wh.is_default:
        flash("لا يمكن تعطيل المخزن الافتراضي", "error")
    else:
        wh.is_active = not wh.is_active
        db.session.commit()
        flash("تم التحديث", "success")
    return redirect(url_for("inventory.warehouses"))


# ─── Movement log (the audit surface) ────────────────────────────────────
@bp.route("/movements/")
@login_required
@require_permission("inventory.view")
def movements():
    cid = g.active_company.id
    q = StockMovement.query.filter_by(company_id=cid)

    user_id = request.args.get("user_id", type=int)
    if user_id:
        q = q.filter(StockMovement.actor_id == user_id)
    kind = request.args.get("kind")
    if kind:
        q = q.filter(StockMovement.kind == kind)
    variant_id = request.args.get("variant_id", type=int)
    if variant_id:
        q = q.filter(StockMovement.variant_id == variant_id)
    warehouse_id = request.args.get("warehouse_id", type=int)
    if warehouse_id:
        q = q.filter(StockMovement.warehouse_id == warehouse_id)
    from_raw = request.args.get("from")
    if from_raw:
        try:
            d = datetime.strptime(from_raw, "%Y-%m-%d")
            q = q.filter(StockMovement.created_at >= d)
        except ValueError:
            pass
    to_raw = request.args.get("to")
    if to_raw:
        try:
            d = datetime.strptime(to_raw, "%Y-%m-%d") + timedelta(days=1)
            q = q.filter(StockMovement.created_at < d)
        except ValueError:
            pass

    rows = q.order_by(StockMovement.created_at.desc()).limit(500).all()

    # MARSOUD-SOURCE-REFERENCE-01 (Abdelhamid 2026-07-25) — batched
    # (source_type, source_id) → {label, url} map so the template
    # can render a clickable reference without N+1 queries.
    from app.services.source_reference import (
        build_reference_map, UNKNOWN_LABEL,
    )
    ref_map = build_reference_map(rows, cid)

    return render_template(
        "inventory/movements.html",
        movements=rows,
        ref_map=ref_map,
        unknown_label=UNKNOWN_LABEL,
        kinds=StockMovementKind,
        users=_company_users(cid),
        variants=ProductVariant.query.filter_by(
            company_id=cid, is_active=True).order_by(ProductVariant.sku).all(),
        warehouses=Warehouse.query.filter_by(
            company_id=cid, is_active=True).order_by(Warehouse.code).all(),
        kind_filter=kind,
        user_filter=user_id,
        variant_filter=variant_id,
        warehouse_filter=warehouse_id,
        from_filter=from_raw,
        to_filter=to_raw,
    )


# ─── Adjustments ─────────────────────────────────────────────────────────
@bp.route("/adjust", methods=["GET", "POST"])
@login_required
@require_permission("inventory.manage")
def adjust():
    cid = g.active_company.id
    if request.method == "POST":
        try:
            vid = int(request.form.get("variant_id"))
            wid = int(request.form.get("warehouse_id"))
            new_qty = float(request.form.get("new_qty"))
            reason = (request.form.get("reason") or "").strip()
            if not reason:
                raise InventoryError("سبب التسوية مطلوب")
            variant = db.session.get(ProductVariant, vid)
            wh = db.session.get(Warehouse, wid)
            if not variant or variant.company_id != cid:
                raise InventoryError("الصنف غير صالح")
            if not wh or wh.company_id != cid:
                raise InventoryError("المخزن غير صالح")
            record_adjustment(
                variant=variant, warehouse=wh, new_qty=new_qty,
                reason=reason, actor_id=current_user.id,
                created_by=current_user.id,
            )
            db.session.commit()
            flash("تم حفظ التسوية وقيد فرق الجرد", "success")
            return redirect(url_for("inventory.movements"))
        except (InventoryError, ValueError, TypeError) as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template(
        "inventory/adjust.html",
        variants=ProductVariant.query.filter_by(
            company_id=cid, is_active=True).order_by(ProductVariant.sku).all(),
        warehouses=Warehouse.query.filter_by(
            company_id=cid, is_active=True).order_by(Warehouse.code).all(),
    )


# ─── Opening balance ─────────────────────────────────────────────────────
@bp.route("/opening-balance", methods=["GET", "POST"])
@login_required
@require_permission("inventory.manage")
def opening_balance():
    cid = g.active_company.id
    if request.method == "POST":
        try:
            vid = int(request.form.get("variant_id"))
            wid = int(request.form.get("warehouse_id"))
            qty = float(request.form.get("qty"))
            unit_cost = float(request.form.get("unit_cost"))
            variant = db.session.get(ProductVariant, vid)
            wh = db.session.get(Warehouse, wid)
            if not variant or variant.company_id != cid:
                raise InventoryError("الصنف غير صالح")
            if not wh or wh.company_id != cid:
                raise InventoryError("المخزن غير صالح")
            record_opening_balance(
                variant=variant, warehouse=wh,
                qty=qty, unit_cost=unit_cost,
                actor_id=current_user.id, created_by=current_user.id,
                reason="رصيد افتتاحي",
            )
            db.session.commit()
            flash("تم إدخال الرصيد الافتتاحي وقيد المحاسبة", "success")
            return redirect(url_for("inventory.index"))
        except (InventoryError, ValueError, TypeError) as e:
            db.session.rollback()
            flash(str(e), "error")
    # Show only variants that have NO movements yet — opening is one-shot.
    candidates = []
    for v in ProductVariant.query.filter_by(
        company_id=cid, is_active=True,
    ).all():
        has_mv = StockMovement.query.filter_by(variant_id=v.id).first()
        if not has_mv:
            candidates.append(v)
    return render_template(
        "inventory/opening_balance.html",
        variants=candidates,
        warehouses=Warehouse.query.filter_by(
            company_id=cid, is_active=True).order_by(Warehouse.code).all(),
    )


# ─── Variant detail ──────────────────────────────────────────────────────
@bp.route("/variants/<int:variant_id>")
@login_required
@require_permission("inventory.view")
def variant_detail(variant_id):
    cid = g.active_company.id
    v = db.session.get(ProductVariant, variant_id)
    if not v or v.company_id != cid:
        abort(404)
    balances = StockBalance.query.filter_by(variant_id=v.id).all()
    movements = StockMovement.query.filter_by(variant_id=v.id).order_by(
        StockMovement.created_at.desc()).limit(100).all()
    return render_template(
        "inventory/variant_detail.html",
        variant=v, balances=balances, movements=movements,
    )


# ─── Recompute / drift check ─────────────────────────────────────────────
@bp.route("/movements/recheck", methods=["POST"])
@login_required
@require_permission("inventory.manage")
def recheck():
    cid = g.active_company.id
    drift = []
    for v in ProductVariant.query.filter_by(company_id=cid).all():
        for w in Warehouse.query.filter_by(company_id=cid).all():
            r = recompute_balance(v, w)
            if not r["ok"] and (r["qty"] != 0 or r["cached_qty"] != 0):
                drift.append((v.sku, w.code, r))
    if drift:
        flash(f"وجدنا {len(drift)} اختلاف بين السجل والكاش — راجع السجل",
              "warning")
    else:
        flash("لا يوجد اختلاف — كل الأرصدة متطابقة مع السجل", "success")
    return redirect(url_for("inventory.index"))


# ─── Transfers (ERP-03) ──────────────────────────────────────────────────
@bp.route("/transfers/")
@login_required
@require_permission("transfers.view")
def transfers():
    cid = g.active_company.id
    from app.models import StockTransfer
    status_filter = request.args.get("status")
    q = StockTransfer.query.filter_by(company_id=cid)
    if status_filter:
        q = q.filter(StockTransfer.status == status_filter)
    rows = q.order_by(StockTransfer.created_at.desc()).limit(200).all()
    return render_template("inventory/transfers_index.html",
                           transfers=rows,
                           status_filter=status_filter)


@bp.route("/transfers/new", methods=["GET", "POST"])
@login_required
@require_permission("transfers.manage")
def transfer_new():
    import json
    cid = g.active_company.id
    if request.method == "POST":
        from app.services.inventory_transfers import (
            create_transfer, TransferError,
        )
        try:
            from_id = int(request.form.get("from_warehouse_id"))
            to_id = int(request.form.get("to_warehouse_id"))
            items = json.loads(request.form.get("items_json") or "[]")
            tr = create_transfer(
                company_id=cid,
                from_warehouse_id=from_id, to_warehouse_id=to_id,
                items=items,
                created_by_id=current_user.id,
                notes=request.form.get("notes"),
            )
            flash(f"تم إنشاء التحويل {tr.number} (مسودة)", "success")
            return redirect(url_for("inventory.transfer_detail",
                                    transfer_id=tr.id))
        except (TransferError, ValueError, TypeError, KeyError) as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template(
        "inventory/transfer_form.html",
        warehouses=Warehouse.query.filter_by(
            company_id=cid, is_active=True).order_by(Warehouse.code).all(),
        variants=ProductVariant.query.filter_by(
            company_id=cid, is_active=True).order_by(ProductVariant.sku).all(),
    )


@bp.route("/transfers/<int:transfer_id>")
@login_required
@require_permission("transfers.view")
def transfer_detail(transfer_id):
    from app.models import StockTransfer
    tr = db.session.get(StockTransfer, transfer_id)
    if not tr or tr.company_id != g.active_company.id:
        abort(404)
    return render_template("inventory/transfer_detail.html", transfer=tr)


@bp.route("/transfers/<int:transfer_id>/post", methods=["POST"])
@login_required
@require_permission("transfers.manage")
def transfer_post(transfer_id):
    from app.models import StockTransfer
    from app.services.inventory_transfers import post_transfer, TransferError
    tr = db.session.get(StockTransfer, transfer_id)
    if not tr or tr.company_id != g.active_company.id:
        abort(404)
    try:
        post_transfer(tr, posted_by_id=current_user.id)
        flash(f"تم تنفيذ التحويل {tr.number}", "success")
    except TransferError as e:
        flash(str(e), "error")
    return redirect(url_for("inventory.transfer_detail", transfer_id=tr.id))


@bp.route("/transfers/<int:transfer_id>/cancel", methods=["POST"])
@login_required
@require_permission("transfers.manage")
def transfer_cancel(transfer_id):
    from app.models import StockTransfer
    from app.services.inventory_transfers import cancel_transfer, TransferError
    tr = db.session.get(StockTransfer, transfer_id)
    if not tr or tr.company_id != g.active_company.id:
        abort(404)
    try:
        cancel_transfer(tr, cancelled_by_id=current_user.id,
                        reason=request.form.get("reason"))
        flash("تم إلغاء التحويل", "success")
    except TransferError as e:
        flash(str(e), "error")
    return redirect(url_for("inventory.transfer_detail", transfer_id=tr.id))


# ─── Barcode print (ERP-03) ──────────────────────────────────────────────
@bp.route("/variants/<int:variant_id>/barcode.png")
@login_required
@require_permission("inventory.view")
def variant_barcode(variant_id):
    from flask import send_file
    from app.services.barcodes import (
        generate_barcode_png, assign_barcode_if_missing, BarcodeError,
    )
    v = db.session.get(ProductVariant, variant_id)
    if not v or v.company_id != g.active_company.id:
        abort(404)
    value = assign_barcode_if_missing(v)
    try:
        buf = generate_barcode_png(value)
    except BarcodeError as e:
        abort(500)
    return send_file(buf, mimetype="image/png",
                     download_name=f"{v.sku}.png")


@bp.route("/barcodes/picker")
@login_required
@require_permission("inventory.manage")
def barcodes_picker():
    cid = g.active_company.id
    variants = ProductVariant.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(ProductVariant.sku).all()
    return render_template("inventory/barcodes_picker.html",
                           variants=variants)


@bp.route("/barcodes/print")
@login_required
@require_permission("inventory.manage")
def barcodes_print():
    cid = g.active_company.id
    from app.services.barcodes import variants_for_print_sheet
    ids_raw = request.args.get("ids", "")
    try:
        ids = [int(x) for x in ids_raw.split(",") if x.strip()]
    except ValueError:
        ids = []
    variants = variants_for_print_sheet(cid, ids)
    cols = int(request.args.get("cols") or 4)
    rows = int(request.args.get("rows") or 6)
    return render_template("inventory/barcodes_sheet.html",
                           variants=variants, cols=cols, rows=rows)


# ─── Exports (ERP-02) ────────────────────────────────────────────────────
@bp.route("/reports/balance")
@login_required
@require_permission("inventory.view")
def inventory_balance():
    """Current per-warehouse stock balance — the report version of the
    dashboard. Same data, different presentation (full table).

    Optional ?warehouse_id=X scopes the whole report (screen + exports)
    to a single warehouse — كشف حساب مخزن واحد.
    """
    cid = g.active_company.id
    warehouse_id = request.args.get("warehouse_id", type=int)
    from app.services.export import _inventory_balance_rows
    rows = _inventory_balance_rows(cid, warehouse_id=warehouse_id)
    total = sum(r["value"] for r in rows)
    return render_template(
        "inventory/balance_report.html",
        rows=rows, total_value=total,
        warehouses=Warehouse.query.filter_by(
            company_id=cid, is_active=True).order_by(Warehouse.code).all(),
        warehouse_filter=warehouse_id,
    )


def _generic_export(report_type, fmt, start=None, end=None, **kwargs):
    from flask import send_file
    from app.services.export import export_report
    buf, filename, mimetype = export_report(
        g.active_company, report_type, fmt,
        start or date.today().replace(day=1), end or date.today(),
        **kwargs,
    )
    return send_file(buf, as_attachment=True,
                     download_name=filename, mimetype=mimetype)


def _date_range_from_query():
    from_raw = request.args.get("from") or request.args.get("start_date")
    to_raw = request.args.get("to") or request.args.get("end_date")
    today = date.today()
    try:
        start = datetime.strptime(from_raw, "%Y-%m-%d").date() if from_raw else today.replace(day=1)
    except ValueError:
        start = today.replace(day=1)
    try:
        end = datetime.strptime(to_raw, "%Y-%m-%d").date() if to_raw else today
    except ValueError:
        end = today
    return start, end


@bp.route("/reports/low-stock.<fmt>")
@login_required
@require_permission("inventory.view")
def export_low_stock(fmt):
    if fmt not in ("xlsx", "pdf"):
        abort(404)
    return _generic_export("low-stock", "pdf" if fmt == "pdf" else "excel")


@bp.route("/reports/movement-log.<fmt>")
@login_required
@require_permission("inventory.view")
def export_movements(fmt):
    if fmt not in ("xlsx", "pdf"):
        abort(404)
    start, end = _date_range_from_query()
    return _generic_export("stock-movements",
                           "pdf" if fmt == "pdf" else "excel",
                           start, end)


@bp.route("/reports/balance.<fmt>")
@login_required
@require_permission("inventory.view")
def export_inventory_balance(fmt):
    if fmt not in ("xlsx", "pdf"):
        abort(404)
    warehouse_id = request.args.get("warehouse_id", type=int)
    return _generic_export("inventory-balance",
                           "pdf" if fmt == "pdf" else "excel",
                           warehouse_id=warehouse_id)


# ─── Internal helpers ────────────────────────────────────────────────────
def _company_users(company_id):
    from app.models.user import user_companies
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == company_id)
    ).fetchall()
    return [db.session.get(User, r.user_id) for r in rows]
