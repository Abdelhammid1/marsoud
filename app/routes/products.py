from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, g, abort
from flask_login import login_required, current_user
from app import db
from app.models import (
    Product, ProductVariant, Warehouse, StockMovement, StockBalance,
    ProductGroup, ProductCategory,
)
from app.services.inventory import record_opening_balance, default_warehouse
from app.services.permissions import require_permission

bp = Blueprint("products", __name__)


def _ensure_default_hierarchy(company_id):
    """MARSOUD-PRODUCT-HIERARCHY-01 — a company created AFTER the
    migration still needs a default 'عام' group/category so its first
    product save doesn't fail category-required validation.

    MARSOUD-HIERARCHY-DELETE-STICKS (Abdelhamid 2026-07-22) — used to
    always re-create the 'عام' row on every visit. That silently
    undid an owner's delete: the flash said "تم الحذف" then the next
    render put the row back. Now we only seed the default when the
    company has ZERO groups. Once the owner has any group of their
    own, delete of 'عام' sticks.
    """
    existing = ProductGroup.query.filter_by(
        company_id=company_id).first()
    if existing:
        # Even if 'عام' is gone, another group can catch new products
        # via the /hierarchy page — no need to recreate it.
        return existing, None
    g_row = ProductGroup(company_id=company_id, name="عام")
    db.session.add(g_row); db.session.flush()
    c_row = ProductCategory(
        company_id=company_id, group_id=g_row.id, name="عام",
    )
    db.session.add(c_row); db.session.flush()
    return g_row, c_row


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
    cid = g.active_company.id
    # MARSOUD-PRODUCT-HIERARCHY-01 — optional group / category filters.
    q = Product.query.filter_by(company_id=cid)
    group_id = request.args.get("group_id", type=int)
    category_id = request.args.get("category_id", type=int)
    if category_id:
        q = q.filter(Product.category_id == category_id)
    elif group_id:
        # Filter by every category under the chosen group.
        cat_ids = [c.id for c in ProductCategory.query.filter_by(
            company_id=cid, group_id=group_id,
        ).all()]
        if cat_ids:
            q = q.filter(Product.category_id.in_(cat_ids))
        else:
            q = q.filter(db.false())   # group has no categories
    products = q.order_by(Product.name).all()
    groups = ProductGroup.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(ProductGroup.name).all()
    categories = ProductCategory.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(ProductCategory.name).all()
    return render_template(
        "products/index.html",
        products=products, groups=groups, categories=categories,
        selected_group_id=group_id, selected_category_id=category_id,
    )


def _generate_variant_sku(company_id, product_id):
    """Build a fallback SKU when the user leaves it blank.

    PER-CO-NUMBERING (Abdelhamid 2026-07-04) — uses the shared
    next_number() infra so each company's SKUs count from PRD-0001,
    not from PRD-<global product_id> which leaks the global PK.
    Falls back to PRD-<id> only if next_number fails for any reason
    (defensive; shouldn't happen in production)."""
    from app.services.numbering import next_number
    try:
        return next_number(company_id, "PRODUCT")
    except Exception:
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

            # MARSOUD-PRODUCT-HIERARCHY-01 — category required.
            category_id = request.form.get("category_id", type=int)
            if not category_id:
                raise ValueError("الفئة مطلوبة — كل منتج يجب أن يكون تحت فئة")
            cat = ProductCategory.query.get(category_id)
            if not cat or cat.company_id != cid:
                raise ValueError("الفئة غير صحيحة")

            # MARSOUD-PACK-ONLY-PRICING — parsed up-front so a bad
            # pricing block fails before anything is written. There is
            # no `default_price` / `unit_cost` form field any more; both
            # are derived by apply_pack_pricing() below.
            from app.services.product_pricing import (
                parse_pack_input, apply_pack_pricing, PricingError,
            )
            pieces, pack_purchase, pack_sale, pack_unit_name = \
                parse_pack_input(request.form, is_goods=is_tracked)

            p = Product(
                company_id=cid,
                name=name,
                description=request.form.get("description", "").strip() or None,
                default_tax_rate=float(request.form.get("default_tax_rate") or 0) or None,
                sku=(request.form.get("sku") or "").strip() or None,
                is_tracked=is_tracked,
                category_id=category_id,
                # MARSOUD-DUAL-UOM-WEIGHT-01 UI — opt-in flag; only
                # meaningful for goods, harmless on services.
                tracks_piece_count=(
                    is_tracked and
                    request.form.get("tracks_piece_count") == "on"),
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

                v = ProductVariant(
                    company_id=cid,
                    product_id=p.id,
                    sku=sku,
                    barcode=barcode,
                    name="",  # blank means "the product itself" in display_name logic
                )
                db.session.add(v)
                db.session.flush()

                # MARSOUD-PACK-ONLY-PRICING — the single place a
                # per-piece cost/price can be set. Also creates the base
                # unit and the pack unit (when the box holds more than
                # one piece), replacing the old inline block below.
                unit_cost, _unit_price = apply_pack_pricing(
                    p, v, pieces=pieces, pack_purchase=pack_purchase,
                    pack_sale=pack_sale, pack_name=pack_unit_name,
                    is_goods=True,
                )
                pieces_per_pack = pieces

                # Opening balance is optional. > 0 → post Dr 1140 / Cr 3900.
                # MARSOUD-PACK-PRICING — the cashier may enter the qty in
                # PACKS ("3 كراتين") instead of base units. Convert here
                # so the ledger sees base units (which is what stock,
                # COGS and every downstream report agree on).
                opening_qty_raw = float(
                    request.form.get("opening_qty", 0) or 0)
                opening_qty_unit = (
                    request.form.get("opening_qty_unit") or "base"
                ).strip()
                if (opening_qty_unit == "pack"
                        and opening_qty_raw > 0):
                    if pieces_per_pack <= 0:
                        raise ValueError(
                            "الرصيد الافتتاحي مطلوب بالعلبة لكن "
                            "لم يتم تحديد عدد القطع في العلبة."
                        )
                    opening_qty = opening_qty_raw * pieces_per_pack
                else:
                    opening_qty = opening_qty_raw
                if opening_qty > 0:
                    if unit_cost <= 0:
                        raise ValueError(
                            "الرصيد الافتتاحي > 0 محتاج تكلفة وحدة > 0",
                        )
                    wh_id = request.form.get("warehouse_id")
                    wh = (db.session.get(Warehouse, int(wh_id))
                          if wh_id else default_warehouse(cid))
                    # MARSOUD-53 — friendlier error pointing the user to the
                    # warehouses page instead of just "المخزن غير صحيح".
                    if not wh:
                        any_wh = Warehouse.query.filter_by(company_id=cid).count()
                        if any_wh == 0:
                            raise ValueError(
                                "الشركة مفيهاش مخزن. أنشئ مخزن أولاً من: "
                                "العمليات ← المخزون ← المخازن.",
                            )
                        raise ValueError("المخزن المختار غير صحيح — اختر مخزن آخر.")
                    if wh.company_id != cid:
                        raise ValueError("المخزن لا ينتمي لهذه الشركة")
                    record_opening_balance(
                        variant=v, warehouse=wh,
                        qty=opening_qty, unit_cost=unit_cost,
                        actor_id=current_user.id, created_by=current_user.id,
                        reason="رصيد افتتاحي من شاشة إضافة المنتج",
                    )
            else:
                # A service has no variant and no units, but it still
                # needs its selling price — same single code path.
                apply_pack_pricing(
                    p, None, pieces=1, pack_purchase=pack_purchase,
                    pack_sale=pack_sale, pack_name=pack_unit_name,
                    is_goods=False,
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

    # MARSOUD-PRODUCT-HIERARCHY-01 — self-heal missing default so the
    # form always has a category the user can pick.
    _ensure_default_hierarchy(cid)
    groups = ProductGroup.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(ProductGroup.name).all()
    categories = ProductCategory.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(ProductCategory.name).all()
    return render_template(
        "products/form.html",
        warehouses=warehouses, groups=groups, categories=categories,
    )


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
            # MARSOUD-PACK-ONLY-PRICING — default_price is derived from
            # the box numbers now, never posted. Same single code path
            # as create.
            from app.services.product_pricing import (
                parse_pack_input, apply_pack_pricing,
            )
            pieces, pack_purchase, pack_sale, pack_unit_name = \
                parse_pack_input(request.form, is_goods=p.is_tracked)
            apply_pack_pricing(
                p, p.default_variant, pieces=pieces,
                pack_purchase=pack_purchase, pack_sale=pack_sale,
                pack_name=pack_unit_name, is_goods=p.is_tracked,
            )
            raw_tax = request.form.get("default_tax_rate")
            p.default_tax_rate = float(raw_tax) if raw_tax not in (None, "", "None") else None
            p.sku = (request.form.get("sku") or "").strip() or None
            p.is_active = (request.form.get("is_active") == "on")
            # MARSOUD-PRODUCT-HIERARCHY-01 — allow reassigning category
            # (historical invoices stay pointed at the product; the ticket
            # says the old invoices don't get rewritten).
            new_cat_id = request.form.get("category_id", type=int)
            if new_cat_id:
                cat = ProductCategory.query.get(new_cat_id)
                if cat and cat.company_id == p.company_id:
                    p.category_id = new_cat_id
            # MARSOUD-DUAL-UOM-WEIGHT-01 UI — flag opt-in / opt-out.
            # Only meaningful when the product is tracked (services
            # ignore it either way).
            if p.is_tracked:
                p.tracks_piece_count = (
                    request.form.get("tracks_piece_count") == "on")
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

    groups = ProductGroup.query.filter_by(
        company_id=p.company_id, is_active=True,
    ).order_by(ProductGroup.name).all()
    categories = ProductCategory.query.filter_by(
        company_id=p.company_id, is_active=True,
    ).order_by(ProductCategory.name).all()
    # MARSOUD-PACK-ONLY-PRICING — prefill the box fields with exactly
    # what was entered, so reopening a product doesn't silently reprice
    # it.
    from app.services.product_pricing import pack_values_for
    return render_template(
        "products/edit.html", product=p, variant_qtys=variant_qtys,
        groups=groups, categories=categories,
        pack=pack_values_for(p),
    )


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
    """JSON endpoint for invoice form autocomplete.

    MARSOUD-CATEGORY-VISIBILITY-01 — this is the ONLY entry point behind
    the customer-invoice product picker (templates/invoices/form.html
    fetches it once and builds every line's dropdown from the result), so
    the customer-invoice switch is enforced here and nowhere else.
    """
    from app.services.category_visibility import product_visible_clause
    cid = g.active_company.id
    q = Product.query.filter_by(company_id=cid, is_active=True)
    _vis = product_visible_clause(cid, "customer_invoices")
    if _vis is not None:
        q = q.filter(_vis)
    products = q.order_by(Product.name).all()
    return jsonify([
        {
            "id": p.id, "name": p.name, "description": p.description or "",
            "price": float(p.default_price or 0),
            "tax_rate": float(p.default_tax_rate) if p.default_tax_rate is not None else None,
            # MARSOUD-UNIT-CONVERSION-01 — bundle each product's units
            # so the invoice-item row can populate its unit dropdown
            # without a follow-up API call.
            "units": [
                {"id": u.id, "name": u.unit_name,
                 "factor": float(u.conversion_factor or 1),
                 "is_base": bool(u.is_base)}
                for u in p.units
            ],
        } for p in products
    ])


# ─── MARSOUD-PRODUCT-HIERARCHY-01 — Group/Category CRUD ─────────────────
@bp.route("/hierarchy")
@login_required
@require_permission("products.manage")
def hierarchy():
    """Single-page tree of every group + its categories, with inline
    forms for both. Simpler than two separate CRUD pages given the tiny
    volume (a shop rarely has more than ~30 categories)."""
    cid = g.active_company.id
    _ensure_default_hierarchy(cid)
    groups = ProductGroup.query.filter_by(company_id=cid).order_by(
        ProductGroup.name,
    ).all()
    # Product counts per category for the delete-guard hint.
    counts = dict(db.session.query(
        Product.category_id, db.func.count(Product.id),
    ).filter(Product.company_id == cid).group_by(
        Product.category_id,
    ).all())
    # MARSOUD-CATEGORY-VISIBILITY-01 — the four switches, ordered and
    # labelled from the registry so adding a module never means editing
    # this template.
    from app.services.category_visibility import MODULES, MODULE_ICONS
    vis_modules = [
        (col, label, MODULE_ICONS.get(key, ""))
        for key, (col, label) in MODULES.items()
    ]
    return render_template(
        "products/hierarchy.html", groups=groups, counts=counts,
        vis_modules=vis_modules,
    )


@bp.route("/hierarchy/groups", methods=["POST"])
@login_required
@require_permission("products.manage")
def group_create():
    cid = g.active_company.id
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("اسم المجموعة مطلوب", "error")
        return redirect(url_for("products.hierarchy"))
    if ProductGroup.query.filter_by(company_id=cid, name=name).first():
        flash("مجموعة بنفس الاسم موجودة", "error")
        return redirect(url_for("products.hierarchy"))
    db.session.add(ProductGroup(company_id=cid, name=name))
    db.session.commit()
    flash("تم إنشاء المجموعة", "success")
    return redirect(url_for("products.hierarchy"))


@bp.route("/hierarchy/groups/<int:group_id>/edit", methods=["POST"])
@login_required
@require_permission("products.manage")
def group_edit(group_id):
    cid = g.active_company.id
    g_row = ProductGroup.query.get_or_404(group_id)
    if g_row.company_id != cid:
        abort(404)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("الاسم مطلوب", "error")
        return redirect(url_for("products.hierarchy"))
    g_row.name = name
    db.session.commit()
    flash("تم التحديث", "success")
    return redirect(url_for("products.hierarchy"))


@bp.route("/hierarchy/groups/<int:group_id>/delete", methods=["POST"])
@login_required
@require_permission("products.manage")
def group_delete(group_id):
    cid = g.active_company.id
    g_row = ProductGroup.query.get_or_404(group_id)
    if g_row.company_id != cid:
        abort(404)
    # Refuse if any category under this group has products.
    total_products = Product.query.join(ProductCategory).filter(
        ProductCategory.group_id == g_row.id,
    ).count()
    if total_products:
        flash(
            f"لا يمكن حذف المجموعة — يوجد {total_products} منتج تحتها. "
            f"انقلهم أولاً.", "error",
        )
        return redirect(url_for("products.hierarchy"))
    db.session.delete(g_row)
    db.session.commit()
    flash("تم حذف المجموعة", "success")
    return redirect(url_for("products.hierarchy"))


@bp.route("/hierarchy/categories", methods=["POST"])
@login_required
@require_permission("products.manage")
def category_create():
    cid = g.active_company.id
    name = (request.form.get("name") or "").strip()
    group_id = request.form.get("group_id", type=int)
    if not (name and group_id):
        flash("الاسم والمجموعة مطلوبان", "error")
        return redirect(url_for("products.hierarchy"))
    g_row = ProductGroup.query.get(group_id)
    if not g_row or g_row.company_id != cid:
        abort(404)
    if ProductCategory.query.filter_by(
        company_id=cid, group_id=group_id, name=name,
    ).first():
        flash("فئة بنفس الاسم موجودة تحت هذه المجموعة", "error")
        return redirect(url_for("products.hierarchy"))
    # MARSOUD-CATEGORY-VISIBILITY-01 — the four module switches. The form
    # ships them pre-ticked, so the normal path creates a category that
    # behaves like every existing one.
    from app.services.category_visibility import flags_from_form
    db.session.add(ProductCategory(
        company_id=cid, group_id=group_id, name=name,
        **flags_from_form(request.form),
    ))
    db.session.commit()
    flash("تم إنشاء الفئة", "success")
    return redirect(url_for("products.hierarchy"))


@bp.route("/hierarchy/categories/<int:cat_id>/edit", methods=["POST"])
@login_required
@require_permission("products.manage")
def category_edit(cat_id):
    cid = g.active_company.id
    c = ProductCategory.query.get_or_404(cat_id)
    if c.company_id != cid:
        abort(404)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("الاسم مطلوب", "error")
        return redirect(url_for("products.hierarchy"))
    c.name = name
    # MARSOUD-CATEGORY-VISIBILITY-01 — the switches live in this same
    # form, so one 💾 saves both. flags_from_form reads PRESENCE, because
    # an unticked checkbox is not submitted at all; reading .get() would
    # make unticking impossible to save.
    from app.services.category_visibility import flags_from_form
    for col, value in flags_from_form(request.form).items():
        setattr(c, col, value)
    db.session.commit()
    flash("تم التحديث", "success")
    return redirect(url_for("products.hierarchy"))


@bp.route("/hierarchy/categories/<int:cat_id>/delete", methods=["POST"])
@login_required
@require_permission("products.manage")
def category_delete(cat_id):
    cid = g.active_company.id
    c = ProductCategory.query.get_or_404(cat_id)
    if c.company_id != cid:
        abort(404)
    n = Product.query.filter_by(category_id=c.id).count()
    if n:
        flash(
            f"لا يمكن حذف الفئة — يوجد {n} منتج تحتها. انقلهم أولاً.",
            "error",
        )
        return redirect(url_for("products.hierarchy"))
    db.session.delete(c)
    db.session.commit()
    flash("تم حذف الفئة", "success")
    return redirect(url_for("products.hierarchy"))


# ─── API helper for the dependent dropdown on product form ──────────
@bp.route("/api/categories")
@login_required
def api_categories():
    """Return categories filtered by ?group_id=<id>. Used by product
    form JS to populate the second dropdown when a group is picked."""
    cid = g.active_company.id
    group_id = request.args.get("group_id", type=int)
    q = ProductCategory.query.filter_by(company_id=cid, is_active=True)
    if group_id:
        q = q.filter_by(group_id=group_id)
    return jsonify([
        {"id": c.id, "name": c.name, "group_id": c.group_id}
        for c in q.order_by(ProductCategory.name).all()
    ])


# ─── MARSOUD-UNIT-CONVERSION-01 — Product units ─────────────────────
@bp.route("/<int:product_id>/units", methods=["GET", "POST"])
@login_required
@require_permission("products.manage")
def units(product_id):
    from app.models import ProductUnit
    from app.services.units import (
        ensure_base_unit, create_unit, delete_unit, UnitError,
    )
    p = _product_or_404(product_id)
    if not p.is_tracked:
        flash("الوحدات متاحة للمنتجات المتتبَّعة فقط.", "warning")
        return redirect(url_for("products.edit", product_id=p.id))
    ensure_base_unit(p)   # idempotent — heals any product missing base

    if request.method == "POST":
        try:
            new_unit = create_unit(
                p,
                request.form.get("unit_name"),
                request.form.get("conversion_factor"),
                sale_price=request.form.get("sale_price"),
            )
            # MARSOUD-PACK-ONLY-PRICING — the pack-purchase-cost helper
            # that used to live here (and rewrote variant.unit_cost from
            # this page) is gone. Cost now has exactly one owner: the
            # box purchase price on the product form. Two screens
            # writing the same number could only disagree.
            db.session.commit()
            flash("تم إضافة الوحدة", "success")
        except UnitError as e:
            db.session.rollback()
            flash(str(e), "error")
        return redirect(url_for("products.units", product_id=p.id))

    units_list = ProductUnit.query.filter_by(product_id=p.id).order_by(
        ProductUnit.is_base.desc(),
        ProductUnit.conversion_factor.asc(),
    ).all()
    return render_template(
        "products/units.html", product=p, units=units_list,
    )


@bp.route("/<int:product_id>/units/<int:unit_id>/delete", methods=["POST"])
@login_required
@require_permission("products.manage")
def unit_delete(product_id, unit_id):
    from app.models import ProductUnit
    from app.services.units import delete_unit, UnitError
    p = _product_or_404(product_id)
    u = db.session.get(ProductUnit, unit_id)
    if not u or u.product_id != p.id:
        abort(404)
    try:
        delete_unit(u)
        db.session.commit()
        flash("تم حذف الوحدة", "success")
    except UnitError as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("products.units", product_id=p.id))


@bp.route("/<int:product_id>/units/<int:unit_id>/edit", methods=["POST"])
@login_required
@require_permission("products.manage")
def unit_edit(product_id, unit_id):
    """MARSOUD-UNIT-CONVERSION-01 — edit the conversion factor.

    Refuses if the unit is base OR has any historical movements
    (via can_edit_factor). Editing a used unit would silently rewrite
    the retroactive math on frozen base_quantity snapshots — never OK.
    """
    from decimal import Decimal
    from app.models import ProductUnit
    from app.services.units import (
        can_edit_factor, set_unit_sale_price, UnitError,
    )
    p = _product_or_404(product_id)
    u = db.session.get(ProductUnit, unit_id)
    if not u or u.product_id != p.id:
        abort(404)
    # MARSOUD-UOM-PRICE — the price field is editable on every unit,
    # base or not; only the name+factor mutation is locked once the
    # unit has movements.
    raw_price = request.form.get("sale_price")
    if u.is_base:
        # MARSOUD-PACK-ONLY-PRICING — the base unit's price is the
        # per-piece price, which is now derived from the box sale price
        # on the product form. This branch used to write it back into
        # Product.default_price, which would be a circular write against
        # a computed value. The row is display-only on the units page;
        # this only fires on a hand-crafted POST.
        flash("سعر القطعة بيتحسب من سعر بيع العلبة — عدّله من صفحة المنتج.",
              "warning")
        return redirect(url_for("products.edit", product_id=p.id))
    if not can_edit_factor(u):
        # Even for a used unit we still allow sale_price mutation —
        # it doesn't rewrite any historical base_quantity snapshots.
        try:
            set_unit_sale_price(u, raw_price)
            db.session.commit()
            flash("تم تحديث السعر (الاسم والمعامل مقفولين لوجود حركات).",
                  "success")
        except UnitError as e:
            db.session.rollback()
            flash(str(e), "error")
        return redirect(url_for("products.units", product_id=p.id))
    new_name = (request.form.get("unit_name") or "").strip()
    raw_factor = request.form.get("conversion_factor")
    try:
        new_factor = Decimal(str(raw_factor)) if raw_factor else None
    except Exception:
        new_factor = None
    if not new_name or new_factor is None or new_factor <= 0:
        flash("اسم الوحدة ومعامل التحويل مطلوبان", "error")
        return redirect(url_for("products.units", product_id=p.id))
    dup = ProductUnit.query.filter(
        ProductUnit.product_id == p.id,
        db.func.lower(ProductUnit.unit_name) == new_name.lower(),
        ProductUnit.id != u.id,
    ).first()
    if dup:
        flash("وحدة بنفس الاسم موجودة على المنتج", "error")
        return redirect(url_for("products.units", product_id=p.id))
    u.unit_name = new_name
    u.conversion_factor = new_factor
    try:
        set_unit_sale_price(u, raw_price)
    except UnitError as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("products.units", product_id=p.id))
    db.session.commit()
    flash("تم تحديث الوحدة", "success")
    return redirect(url_for("products.units", product_id=p.id))


@bp.route("/api/units")
@login_required
def api_units():
    """MARSOUD-UNIT-CONVERSION-01 — units for a given product, used by
    the invoice/vendor-bill/POS item rows to build the unit dropdown.
    Query: ?product_id=<id>"""
    from app.models import ProductUnit
    cid = g.active_company.id
    product_id = request.args.get("product_id", type=int)
    if not product_id:
        return jsonify([])
    units_q = ProductUnit.query.filter_by(
        company_id=cid, product_id=product_id,
    ).order_by(ProductUnit.is_base.desc(),
                 ProductUnit.conversion_factor.asc())
    return jsonify([
        {
            "id": u.id, "name": u.unit_name,
            "factor": float(u.conversion_factor or 1),
            "is_base": bool(u.is_base),
            "label": u.display_label,
        } for u in units_q.all()
    ])
