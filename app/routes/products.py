from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, g, abort
from flask_login import login_required, current_user
from app import db
from app.models import Product, ProductVariant, Warehouse, StockMovement, StockBalance
from app.services.inventory import record_opening_balance, default_warehouse
from app.services.permissions import require_permission

bp = Blueprint("products", __name__)


def _product_or_404(product_id):
    p = db.session.get(Product, product_id)
    if not p or p.company_id != g.active_company.id:
        abort(404)
    return p


def _variant_or_404(product, variant_id):
    v = db.session.get(ProductVariant, variant_id)
    if not v or v.product_id != product.id:
        abort(404)
    return v


@bp.route("/")
@login_required
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    products = Product.query.filter_by(company_id=g.active_company.id).order_by(Product.name).all()
    return render_template("products/index.html", products=products)


def _generate_variant_sku(company_id, product_id):
    """Build a fallback SKU when the user leaves it blank. Format: PRD-<id>."""
    return f"PRD-{product_id:05d}"


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("products.manage")
def new():
    cid = g.active_company.id
    warehouses = Warehouse.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(Warehouse.is_default.desc(), Warehouse.name).all()

    if request.method == "POST":
        try:
            ptype = (request.form.get("product_type") or "goods").strip()
            is_tracked = (ptype == "goods")

            name = request.form.get("name", "").strip()
            if not name:
                raise ValueError("الاسم مطلوب")

            p = Product(
                company_id=cid,
                name=name,
                description=request.form.get("description", "").strip() or None,
                default_price=float(request.form.get("default_price", 0) or 0),
                default_tax_rate=float(request.form.get("default_tax_rate") or 0) or None,
                sku=(request.form.get("sku") or "").strip() or None,
                is_tracked=is_tracked,
            )
            db.session.add(p)
            db.session.flush()  # need p.id for the variant SKU fallback

            # MARSOUD-50.1 — every "بضاعة" product gets one default variant on
            # creation. Without this, the product is unsellable: the invoice
            # posting path requires invoice_items.variant_id NOT NULL when
            # the product is_tracked.
            if is_tracked:
                sku = p.sku or _generate_variant_sku(cid, p.id)
                # Variant SKU must be unique per company. If the user typed an
                # SKU that already exists on another variant, fall back to PRD-id.
                if ProductVariant.query.filter_by(
                    company_id=cid, sku=sku,
                ).first() is not None:
                    sku = _generate_variant_sku(cid, p.id)
                p.sku = sku  # mirror on product for legacy lookups

                barcode = (request.form.get("barcode") or "").strip() or None
                if barcode and ProductVariant.query.filter_by(
                    company_id=cid, barcode=barcode,
                ).first() is not None:
                    raise ValueError(f"الباركود '{barcode}' موجود بالفعل لمنتج آخر")

                unit_cost = float(request.form.get("unit_cost", 0) or 0)
                v = ProductVariant(
                    company_id=cid,
                    product_id=p.id,
                    sku=sku,
                    barcode=barcode,
                    name="",  # blank means "the product itself" in display_name logic
                    unit_cost=unit_cost,
                )
                db.session.add(v)
                db.session.flush()

                # Opening balance is optional. > 0 → post Dr 1140 / Cr 3900.
                opening_qty = float(request.form.get("opening_qty", 0) or 0)
                if opening_qty > 0:
                    if unit_cost <= 0:
                        raise ValueError(
                            "الرصيد الافتتاحي > 0 محتاج تكلفة وحدة > 0",
                        )
                    wh_id = request.form.get("warehouse_id")
                    wh = (db.session.get(Warehouse, int(wh_id))
                          if wh_id else default_warehouse(cid))
                    if not wh or wh.company_id != cid:
                        raise ValueError("المخزن غير صحيح")
                    record_opening_balance(
                        variant=v, warehouse=wh,
                        qty=opening_qty, unit_cost=unit_cost,
                        actor_id=current_user.id, created_by=current_user.id,
                        reason="رصيد افتتاحي من شاشة إضافة المنتج",
                    )

            db.session.commit()
            if is_tracked:
                flash(f"تم إضافة البضاعة: {p.name}", "success")
            else:
                flash(f"تم إضافة الخدمة: {p.name}", "success")
            return redirect(url_for("products.index"))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")
        except Exception as e:
            db.session.rollback()
            flash(f"خطأ: {e}", "error")

    return render_template("products/form.html", warehouses=warehouses)


# ─── MARSOUD-50.2: edit page + variant management ──────────────────
@bp.route("/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("products.manage")
def edit(product_id):
    p = _product_or_404(product_id)
    if request.method == "POST":
        try:
            name = (request.form.get("name") or "").strip()
            if not name:
                raise ValueError("الاسم مطلوب")
            p.name = name
            p.description = (request.form.get("description") or "").strip() or None
            p.default_price = float(request.form.get("default_price", 0) or 0)
            raw_tax = request.form.get("default_tax_rate")
            p.default_tax_rate = float(raw_tax) if raw_tax not in (None, "", "None") else None
            p.sku = (request.form.get("sku") or "").strip() or None
            p.is_active = (request.form.get("is_active") == "on")
            db.session.commit()
            flash("تم حفظ التعديلات", "success")
            return redirect(url_for("products.edit", product_id=p.id))
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "error")

    # Variants + their current stock totals, for the page table.
    variant_qtys = {}
    for v in p.variants:
        total = db.session.query(db.func.coalesce(db.func.sum(StockBalance.qty), 0)).filter_by(variant_id=v.id).scalar()
        variant_qtys[v.id] = float(total or 0)

    return render_template("products/edit.html", product=p, variant_qtys=variant_qtys)


@bp.route("/<int:product_id>/variants/new", methods=["POST"])
@login_required
@require_permission("products.manage")
def variant_new(product_id):
    p = _product_or_404(product_id)
    if not p.is_tracked:
        flash("لا يمكن إضافة variants لخدمة. غيّر النوع من فورم إنشاء جديد.", "error")
        return redirect(url_for("products.edit", product_id=p.id))
    try:
        sku = (request.form.get("sku") or "").strip()
        if not sku:
            raise ValueError("SKU مطلوب")
        if ProductVariant.query.filter_by(company_id=p.company_id, sku=sku).first():
            raise ValueError(f"SKU '{sku}' مستخدم بالفعل")
        barcode = (request.form.get("barcode") or "").strip() or None
        if barcode and ProductVariant.query.filter_by(company_id=p.company_id, barcode=barcode).first():
            raise ValueError(f"الباركود '{barcode}' مستخدم بالفعل")
        v = ProductVariant(
            company_id=p.company_id, product_id=p.id,
            sku=sku, barcode=barcode,
            name=(request.form.get("name") or "").strip(),
            unit_cost=float(request.form.get("unit_cost", 0) or 0),
            reorder_level=float(request.form.get("reorder_level", 0) or 0),
        )
        db.session.add(v)
        db.session.commit()
        flash(f"تم إضافة variant '{sku}'", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("products.edit", product_id=p.id))


@bp.route("/<int:product_id>/variants/<int:variant_id>/edit", methods=["POST"])
@login_required
@require_permission("products.manage")
def variant_edit(product_id, variant_id):
    p = _product_or_404(product_id)
    v = _variant_or_404(p, variant_id)
    try:
        new_sku = (request.form.get("sku") or "").strip()
        if not new_sku:
            raise ValueError("SKU مطلوب")
        if new_sku != v.sku:
            if ProductVariant.query.filter_by(company_id=p.company_id, sku=new_sku).first():
                raise ValueError(f"SKU '{new_sku}' مستخدم بالفعل")
        new_barcode = (request.form.get("barcode") or "").strip() or None
        if new_barcode and new_barcode != v.barcode:
            if ProductVariant.query.filter_by(company_id=p.company_id, barcode=new_barcode).first():
                raise ValueError(f"الباركود '{new_barcode}' مستخدم بالفعل")
        v.sku = new_sku
        v.barcode = new_barcode
        v.name = (request.form.get("name") or "").strip()
        v.unit_cost = float(request.form.get("unit_cost", 0) or 0)
        v.reorder_level = float(request.form.get("reorder_level", 0) or 0)
        db.session.commit()
        flash(f"تم تعديل variant '{v.sku}'", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("products.edit", product_id=p.id))


@bp.route("/<int:product_id>/variants/<int:variant_id>/deactivate", methods=["POST"])
@login_required
@require_permission("products.manage")
def variant_deactivate(product_id, variant_id):
    p = _product_or_404(product_id)
    v = _variant_or_404(p, variant_id)
    # Refuse if it has stock movements — deactivation is the right action,
    # not hard delete.
    if not v.is_active:
        v.is_active = True
        flash(f"تم إعادة تفعيل variant '{v.sku}'", "success")
    else:
        v.is_active = False
        flash(f"تم تعطيل variant '{v.sku}'", "success")
    db.session.commit()
    return redirect(url_for("products.edit", product_id=p.id))


@bp.route("/api/list")
@login_required
def api_list():
    """JSON endpoint for invoice form autocomplete."""
    products = Product.query.filter_by(
        company_id=g.active_company.id, is_active=True
    ).order_by(Product.name).all()
    return jsonify([
        {
            "id": p.id, "name": p.name, "description": p.description or "",
            "price": float(p.default_price or 0),
            "tax_rate": float(p.default_tax_rate) if p.default_tax_rate is not None else None,
        } for p in products
    ])
