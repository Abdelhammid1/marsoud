from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, redirect, url_for, flash, request, g, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import (
    VendorBill, VendorBillItem, VendorBillStatus, VendorBillPaymentMethod,
    BillLineType, Vendor, Account, PaymentMethod,
    Product, ProductVariant, Warehouse,
)
from app.services.vendor_bills import (
    post_vendor_bill, record_bill_payment, update_overdue_vendor_bills,
    get_allowed_accounts_for_line_type, post_vendor_bill_refund,
)
from app.models.refund import VendorRefundType
from app.services.ledger import LedgerError
from app.services.numbering import next_number
from app.services.permissions import require_permission

bp = Blueprint("vendor_bills", __name__)


def _next_bill_number(company_id):
    return next_number(company_id, "VENDOR_BILL")


def _safe_float(raw, default=0):
    """MARSOUD-FIX-VENDOR-BILL-FLOAT (Abdelhamid 2026-07-25).

    Coerce a form field to a float without ever raising ValueError.
    Handles the common bug shapes we saw in production:
      · None / empty string → default
      · Whitespace-only ("   ")  → default (previously crashed)
      · Comma-formatted ("1,000") → 1000 (previously crashed)
      · Non-numeric garbage → default (log but don't 500)

    Called from every numeric form-field read on vendor bills.
    """
    if raw is None:
        return default
    s = str(raw).strip()
    if not s:
        return default
    # Common Arabic/EN thousand separator that the browser accepts
    # in numeric inputs on some locales.
    s = s.replace(",", "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


@bp.route("/")
@login_required
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    update_overdue_vendor_bills(g.active_company.id)

    q = VendorBill.query.filter_by(company_id=g.active_company.id)

    # MARSOUD-VBILL-REFUND-STATUS — deleted bills used to be hidden by a
    # hard `deleted_at.is_(None)` filter, so a deleted bill vanished
    # instead of staying as a struck-through record. Same three-way
    # visibility filter customer invoices got in MARSOUD-VOIDED-VISIBLE:
    # `all` (default) keeps them listed with a 🗑️ badge, `active` hides
    # them, `deleted` shows only them. The totals below already exclude
    # them, so the KPI tiles don't inflate.
    deleted_filter = request.args.get("deleted_filter", "all")
    if deleted_filter == "active":
        q = q.filter(VendorBill.deleted_at.is_(None))
    elif deleted_filter == "deleted":
        q = q.filter(VendorBill.deleted_at.isnot(None))
    else:
        deleted_filter = "all"

    status = request.args.get("status")
    if status:
        try:
            q = q.filter_by(status=VendorBillStatus[status])
        except KeyError:
            pass
    search = (request.args.get("search") or "").strip()
    if search:
        like = f"%{search}%"
        q = q.outerjoin(Vendor, VendorBill.vendor_id == Vendor.id).filter(
            db.or_(VendorBill.number.ilike(like), Vendor.name.ilike(like), VendorBill.supplier_invoice_number.ilike(like))
        )

    # MARSOUD-VBILL-SUBCAT-DISPLAY-FILTER (Abdelhamid 2026-07-16) —
    # dedicated vendor filter + per-vendor sub-category filter. Both
    # are optional; invalid ids are silently dropped so a hand-crafted
    # query arg can't break the page.
    vendor_filter = (request.args.get("vendor") or "").strip()
    if vendor_filter:
        try:
            q = q.filter(VendorBill.vendor_id == int(vendor_filter))
        except (TypeError, ValueError):
            pass
    sub_category_filter = (request.args.get("sub_category") or "").strip()
    if sub_category_filter:
        try:
            sc_id = int(sub_category_filter)
            # Filter to bills that have AT LEAST ONE line with this
            # sub-category. Use EXISTS so we don't dupe-multiply the
            # per-bill totals via a join.
            q = q.filter(
                db.session.query(VendorBillItem.id)
                .filter(VendorBillItem.bill_id == VendorBill.id)
                .filter(VendorBillItem.sub_category_id == sc_id)
                .exists()
            )
        except (TypeError, ValueError):
            pass

    def _parse_date(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))
    if date_from:
        q = q.filter(VendorBill.issue_date >= date_from)
    if date_to:
        q = q.filter(VendorBill.issue_date <= date_to)

    bills = q.order_by(VendorBill.issue_date.desc(), VendorBill.id.desc()).all()

    # MARSOUD-VBILL-REFUND-STATUS — same exclusion pattern as the
    # customer side (routes/invoices.py). Only `outstanding` used to
    # exclude anything, so CANCELLED and refunded bills inflated
    # إجمالي المشتريات and إجمالي المدفوع even though the money came back.
    #
    # `deleted_at` is checked as well as the status, which matters here
    # in a way it doesn't for invoices: a soft-deleted DRAFT vendor bill
    # keeps its DRAFT status, so a status list alone would let it into
    # the totals now that deleted rows are visible.
    _EXCLUDED = (VendorBillStatus.CANCELLED, VendorBillStatus.REFUNDED)
    countable = [b for b in bills
                 if b.deleted_at is None and b.status not in _EXCLUDED]

    totals = {
        # Row count stays the full filtered list — the KPI tiles are
        # billable-only, but users expect to see every row they filtered.
        "count": len(bills),
        "invoiced": sum(float(b.total or 0) for b in countable),
        "paid": sum(float(b.paid_amount or 0) for b in countable),
        "outstanding": sum(b.balance for b in countable),
    }
    # Dropdown feeds — active vendors + sub-categories for the picked
    # vendor. When no vendor is selected we show sub-categories across
    # every vendor (useful when the user picks a sub-cat first).
    vendors_dd = Vendor.query.filter_by(
        company_id=g.active_company.id, is_active=True,
    ).order_by(Vendor.name).all()
    from app.models import VendorSubCategory
    sub_cats_q = VendorSubCategory.query.filter_by(
        company_id=g.active_company.id, is_active=True,
    )
    if vendor_filter:
        try:
            sub_cats_q = sub_cats_q.filter_by(vendor_id=int(vendor_filter))
        except (TypeError, ValueError):
            pass
    sub_cats_dd = sub_cats_q.order_by(VendorSubCategory.name).all()
    return render_template(
        "vendor_bills/index.html",
        bills=bills, statuses=VendorBillStatus, totals=totals,
        vendors=vendors_dd, sub_categories=sub_cats_dd,
        vendor_filter=vendor_filter,
        sub_category_filter=sub_category_filter,
        deleted_filter=deleted_filter,
    )


@bp.route("/api/accounts")
@login_required
def api_accounts():
    """Return accounts allowed for a given line_type. Used by the bill form."""
    try:
        lt = BillLineType[request.args.get("line_type", "EXPENSE")]
    except KeyError:
        return jsonify([])
    accounts = get_allowed_accounts_for_line_type(g.active_company.id, lt)
    return jsonify([
        {"id": a.id, "code": a.code, "name": a.name_ar or a.name} for a in accounts
    ])


@bp.route("/api/inventory-targets")
@login_required
def api_inventory_targets():
    """MARSOUD-50.3 — Returns the variant + warehouse lists the INVENTORY
    line picker needs. Scoped to active company. Only active tracked
    products show their variants."""
    cid = g.active_company.id
    variants = (
        db.session.query(ProductVariant, Product)
        .join(Product, ProductVariant.product_id == Product.id)
        .filter(ProductVariant.company_id == cid,
                ProductVariant.is_active.is_(True),
                Product.is_active.is_(True),
                Product.is_tracked.is_(True))
        .order_by(Product.name, ProductVariant.sku)
        .all()
    )
    warehouses = Warehouse.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(Warehouse.is_default.desc(), Warehouse.name).all()
    return jsonify({
        "variants": [
            {
                "id": v.id, "sku": v.sku,
                "product_id": p.id,
                "label": f"{p.name} — {v.sku}" + (f" ({v.name})" if v.name else ""),
                "unit_cost": float(v.unit_cost or 0),
                # MARSOUD-UNIT-CONVERSION-01 — bundle units so the
                # bill-item row can populate its unit dropdown when a
                # variant is picked, without a follow-up API call.
                "units": [
                    {"id": u.id, "name": u.unit_name,
                     "factor": float(u.conversion_factor or 1),
                     "is_base": bool(u.is_base)}
                    for u in p.units
                ],
            }
            for v, p in variants
        ],
        "warehouses": [
            {"id": w.id, "name": w.name, "is_default": w.is_default}
            for w in warehouses
        ],
    })


def _default_category_id(company_id):
    """MARSOUD-BILL-SPLIT — a category for a product created inline from
    a bill.

    /products/new treats the category as required, but the bill path used
    to leave it NULL, so bill-born products sat outside the hierarchy.
    Prefer any category the company already has; otherwise seed the
    default 'عام' group+category the products screen uses.
    """
    from app.models import ProductCategory, ProductGroup
    cat = ProductCategory.query.filter_by(
        company_id=company_id, is_active=True,
    ).order_by(ProductCategory.id).first()
    if cat:
        return cat.id
    from app.routes.products import _ensure_default_hierarchy
    group, cat = _ensure_default_hierarchy(company_id)
    if cat:
        return cat.id
    # _ensure_default_hierarchy returns (group, None) when the company
    # already has a group but every category under it was deleted.
    if group:
        cat = ProductCategory(company_id=company_id, group_id=group.id,
                              name="عام")
        db.session.add(cat)
        db.session.flush()
        return cat.id
    return None


def _resolve_inventory_target(form, i, company_id):
    """For row i of an INVENTORY line, return (variant, warehouse, unit_id).

    Creates a new Product + ProductVariant inline when the row supplies
    new_product_name + new_product_sku instead of a variant_id.

    MARSOUD-BILL-SPLIT — a product created here used to get no
    ProductUnit at all, so "10 كراتين × 30 قطعة" landed as 10 pieces in
    stock at 30x the per-piece cost, and the product stayed unit-less
    everywhere else until someone opened its units page. Now it goes
    through the same ensure_base_unit + create_unit pair the products
    screen uses (MARSOUD-PACK-PRICING, app/routes/products.py:203), and
    the returned unit_id is stamped on the line so convert_to_base()
    multiplies at posting time.
    """
    variants_in = form.getlist("item_variant_id[]")
    warehouses_in = form.getlist("item_warehouse_id[]")
    new_names = form.getlist("item_new_product_name[]")
    new_skus = form.getlist("item_new_product_sku[]")
    pack_names = form.getlist("item_new_pack_name[]")
    pack_pieces = form.getlist("item_new_pack_pieces[]")
    pack_prices = form.getlist("item_new_pack_sale_price[]")

    wh_id_raw = warehouses_in[i] if i < len(warehouses_in) else ""
    if not wh_id_raw:
        # MARSOUD-53 — if the company has no warehouse at all, the picker
        # was empty, so point the user to fix that root cause.
        any_wh = Warehouse.query.filter_by(company_id=company_id).count()
        if any_wh == 0:
            raise LedgerError(
                "الشركة مفيهاش مخزن. أنشئ مخزن من: العمليات ← المخزون ← المخازن.",
            )
        raise LedgerError("سطر مخزون يحتاج اختيار مخزن")
    wh = db.session.get(Warehouse, int(wh_id_raw))
    if not wh or wh.company_id != company_id:
        raise LedgerError("المخزن المختار غير صحيح")

    new_name = (new_names[i] if i < len(new_names) else "").strip()
    if new_name:
        new_sku = (new_skus[i] if i < len(new_skus) else "").strip()
        if not new_sku:
            raise LedgerError(f"المنتج الجديد '{new_name}' يحتاج SKU")
        if ProductVariant.query.filter_by(company_id=company_id, sku=new_sku).first():
            raise LedgerError(f"SKU '{new_sku}' مستخدم بالفعل لمنتج آخر")
        # Create product + variant inline
        p = Product(
            company_id=company_id, name=new_name,
            is_tracked=True, sku=new_sku,
            category_id=_default_category_id(company_id),
        )
        db.session.add(p)
        db.session.flush()
        v = ProductVariant(
            company_id=company_id, product_id=p.id,
            sku=new_sku, name="",
        )
        db.session.add(v)
        db.session.flush()

        # Units. The base unit is created unconditionally so the product
        # is never unit-less; the pack unit only when the row said how
        # many pieces are in it.
        from app.services.units import (
            ensure_base_unit, create_unit, UnitError,
        )
        base = ensure_base_unit(p)
        unit_id = None
        pieces = _safe_float(
            pack_pieces[i] if i < len(pack_pieces) else None, 0)
        if pieces > 0:
            pack_name = (
                (pack_names[i] if i < len(pack_names) else "") or ""
            ).strip() or "كرتونة"
            if pack_name == base.unit_name:
                raise LedgerError(
                    f"اسم العلبة مطابق لوحدة الأساس — اختر اسم مختلف عن "
                    f"'{base.unit_name}'."
                )
            raw_price = (
                pack_prices[i] if i < len(pack_prices) else "") or ""
            sale_price = (_safe_float(raw_price, 0)
                          if str(raw_price).strip() else None)
            try:
                unit = create_unit(
                    p, unit_name=pack_name, conversion_factor=pieces,
                    sale_price=sale_price,
                )
            except UnitError as e:
                raise LedgerError(str(e))
            unit_id = unit.id
        return v, wh, unit_id

    # Otherwise: existing variant
    v_id_raw = variants_in[i] if i < len(variants_in) else ""
    if not v_id_raw:
        raise LedgerError("سطر مخزون يحتاج اختيار صنف أو إنشاء منتج جديد")
    v = db.session.get(ProductVariant, int(v_id_raw))
    if not v or v.company_id != company_id:
        raise LedgerError("الصنف المختار غير صحيح")
    return v, wh, None


def _populate_from_form(bill, form):
    """Fill bill + items from form data."""
    bill.vendor_id = int(form.get("vendor_id")) if form.get("vendor_id") else None
    bill.supplier_invoice_number = (form.get("supplier_invoice_number") or "").strip()
    bill.issue_date = datetime.strptime(form.get("issue_date") or date.today().isoformat(), "%Y-%m-%d").date()
    bill.due_date = datetime.strptime(form.get("due_date") or (date.today() + timedelta(days=30)).isoformat(), "%Y-%m-%d").date()
    bill.payment_method = VendorBillPaymentMethod[form.get("payment_method", "CASH")]
    bill.notes = form.get("notes", "")
    bill.tax_rate = _safe_float(form.get("tax_rate"), 0)

    # Replace items
    for old in list(bill.items):
        db.session.delete(old)
    db.session.flush()

    descriptions = form.getlist("item_description[]")
    types = form.getlist("item_line_type[]")
    accounts = form.getlist("item_account_id[]")
    quantities = form.getlist("item_quantity[]")
    prices = form.getlist("item_unit_price[]")
    lives = form.getlist("item_useful_life_years[]")
    salvages = form.getlist("item_salvage_value[]")
    # MARSOUD-UNIT-CONVERSION-01 — unit picker on INVENTORY rows.
    unit_ids = form.getlist("item_unit_id[]")
    # MARSOUD-VENDOR-SUBCAT (Abdelhamid 2026-07-14) — per-vendor
    # sub-category on each line. Nullable so old form-posts keep
    # working; validated against the current bill's vendor_id below
    # so a cross-vendor id can't slip in via a hand-crafted POST.
    sub_cat_ids = form.getlist("item_sub_category_id[]")

    for i, desc in enumerate(descriptions):
        if not (desc or "").strip():
            continue
        lt = BillLineType[types[i] if i < len(types) else "EXPENSE"]
        uid_raw = unit_ids[i] if i < len(unit_ids) else None
        try:
            uid = int(uid_raw) if uid_raw else None
        except (TypeError, ValueError):
            uid = None
        # Sub-category id must belong to the same vendor as the bill.
        sc_raw = sub_cat_ids[i] if i < len(sub_cat_ids) else None
        sc_id = None
        try:
            sc_candidate = int(sc_raw) if sc_raw else None
        except (TypeError, ValueError):
            sc_candidate = None
        if sc_candidate and bill.vendor_id:
            from app.models import VendorSubCategory
            row = VendorSubCategory.query.filter_by(
                id=sc_candidate, vendor_id=bill.vendor_id,
                company_id=bill.company_id,
            ).first()
            if row:
                sc_id = row.id
        # MARSOUD-BILL-SPLIT — the inventory form shows no account
        # picker (there is only ever one valid account), so resolve it
        # server-side. Also stops a blank account on any line type from
        # blowing up with a bare ValueError from int("").
        acc_raw = (accounts[i] if i < len(accounts) else "") or ""
        acc_raw = str(acc_raw).strip()
        if acc_raw:
            try:
                acc_id = int(acc_raw)
            except (TypeError, ValueError):
                raise LedgerError(f"حساب البند غير صحيح: {desc.strip()}")
        elif lt == BillLineType.INVENTORY:
            inv_accounts = get_allowed_accounts_for_line_type(
                bill.company_id, BillLineType.INVENTORY)
            if not inv_accounts:
                raise LedgerError(
                    "حساب المخزون (1300) غير موجود — راجع شجرة الحسابات"
                )
            acc_id = inv_accounts[0].id
        else:
            raise LedgerError(f"اختر حساباً للبند: {desc.strip()}")
        item = VendorBillItem(
            bill_id=bill.id,
            description=desc.strip(),
            line_type=lt,
            account_id=acc_id,
            # MARSOUD-FIX-VENDOR-BILL-FLOAT (Abdelhamid 2026-07-25) —
            # raw form values that came in as whitespace-only or
            # comma-formatted ("1,000") used to crash the whole save
            # with ValueError: could not convert string to float: ''.
            # _safe_float handles the edge cases + returns a sane
            # fallback so multi-line saves don't blow up on one
            # sloppy field.
            quantity=_safe_float(quantities[i] if i < len(quantities) else None, 1),
            unit_price=_safe_float(prices[i] if i < len(prices) else None, 0),
            unit_id=uid,
            sub_category_id=sc_id,
        )
        if lt == BillLineType.FIXED_ASSET:
            item.useful_life_years = int(lives[i] or 0) if i < len(lives) and lives[i] else None
            item.salvage_value = _safe_float(
                salvages[i] if i < len(salvages) else None, 0)
        elif lt == BillLineType.INVENTORY:
            # MARSOUD-50.3 — resolve variant + warehouse for INVENTORY lines.
            # Inline-creates a Product+Variant if the row supplied a new name.
            v, wh, new_unit_id = _resolve_inventory_target(
                form, i, bill.company_id)
            item.variant_id = v.id
            item.warehouse_id = wh.id
            # MARSOUD-BILL-SPLIT — a freshly created pack unit isn't in
            # the row's unit picker (that list is built from existing
            # products), so take the one we just made. The quantity the
            # user typed is in packs, and convert_to_base() turns it
            # into pieces at posting time.
            if new_unit_id:
                item.unit_id = new_unit_id
        db.session.add(item)
    db.session.flush()
    bill.items = VendorBillItem.query.filter_by(bill_id=bill.id).all()
    bill.recalc()


# MARSOUD-BILL-SPLIT — the three entry points. One line type per bill:
# each type filters a completely different set of accounts and produces
# a different posting (plain expense / FixedAsset / stock receipt), so a
# single form with a per-row type dropdown just invited mistakes.
# URL slug → BillLineType.
_KIND_SLUGS = {
    "expense": BillLineType.EXPENSE,
    "asset": BillLineType.FIXED_ASSET,
    "inventory": BillLineType.INVENTORY,
}
_SLUG_FOR_TYPE = {v: k for k, v in _KIND_SLUGS.items()}


@bp.route("/new")
@login_required
@require_permission("vendor_bills.create")
def new():
    """Chooser: pick which kind of bill you're recording.

    Kept on the old URL so every existing "فاتورة جديدة" link still
    works (bills list, dashboard, inventory page).
    """
    if not g.active_company:
        return redirect(url_for("companies.new"))

    # MARSOUD-RECURRING-PAY — ?from_recurring=<id> used to land straight
    # on the combined form. Send single-type templates to their own
    # form; a mixed template stops here, since each card now prefills
    # only the lines it owns.
    recurring_id = request.args.get("from_recurring", type=int)
    prefill = _prefill_from_recurring(recurring_id)
    mixed_prefill = False
    if prefill:
        kinds = {it["line_type"] for it in prefill["items"]}
        if len(kinds) == 1:
            only = kinds.pop()
            try:
                slug = _SLUG_FOR_TYPE[BillLineType[only]]
            except KeyError:
                slug = None
            if slug:
                return redirect(url_for("vendor_bills.new_typed", kind=slug,
                                        from_recurring=recurring_id))
        elif len(kinds) > 1:
            mixed_prefill = True

    return render_template(
        "vendor_bills/new_choose.html",
        prefill=prefill, mixed_prefill=mixed_prefill,
        recurring_id=recurring_id,
    )


@bp.route("/new/<kind>", methods=["GET", "POST"])
@login_required
@require_permission("vendor_bills.create")
def new_typed(kind):
    if not g.active_company:
        return redirect(url_for("companies.new"))
    line_type = _KIND_SLUGS.get(kind)
    if not line_type:
        return redirect(url_for("vendor_bills.new"))
    vendors = Vendor.query.filter_by(company_id=g.active_company.id, is_active=True).order_by(Vendor.name).all()

    if request.method == "POST":
        try:
            bill = VendorBill(
                company_id=g.active_company.id,
                number=_next_bill_number(g.active_company.id),
                issue_date=date.today(),
                due_date=date.today() + timedelta(days=30),
                currency=g.active_company.base_currency,
                status=VendorBillStatus.DRAFT,
            )
            db.session.add(bill)
            db.session.flush()
            _populate_from_form(bill, request.form)

            should_post = request.form.get("action") == "post"
            if should_post:
                post_vendor_bill(bill, created_by=current_user.id)
            else:
                db.session.commit()
            flash(f"تم {'إنشاء وتسجيل' if should_post else 'حفظ مسودة'} فاتورة المورد {bill.number}", "success")
            return redirect(url_for("vendor_bills.view", bill_id=bill.id))
        except LedgerError as e:
            db.session.rollback()
            flash(str(e), "error")
        except Exception as e:
            db.session.rollback()
            flash(f"خطأ: {e}", "error")

    # Prefill keeps only the lines this form can actually represent.
    prefill = _prefill_from_recurring(
        request.args.get("from_recurring", type=int))
    if prefill:
        prefill = dict(prefill)
        prefill["items"] = [it for it in prefill["items"]
                            if it["line_type"] == line_type.value]
    return render_template(
        "vendor_bills/new_typed.html", bill=None, vendors=vendors,
        prefill=prefill, kind=line_type.value, kind_slug=kind,
    )


def _prefill_from_recurring(recurring_id):
    """Return a dict the form template's JS can consume to populate
    the vendor / payment method / tax / notes header and the item
    rows from a RecurringBill's source vendor_bill. Returns None
    when the id is missing, unknown, or points to another company."""
    if not recurring_id:
        return None
    from app.models import RecurringBill
    rb = db.session.get(RecurringBill, recurring_id)
    if not rb or rb.company_id != g.active_company.id:
        return None
    src = rb.source_bill
    if not src:
        return None
    return {
        "recurring_id": rb.id,
        "recurring_label": rb.label_ar,
        "vendor_id": src.vendor_id,
        "payment_method": (src.payment_method.value
                            if src.payment_method else "CASH"),
        "tax_rate": float(src.tax_rate or 0),
        "notes": src.notes or "",
        "items": [
            {
                "description": it.description or "",
                "line_type": (it.line_type.value
                               if it.line_type else "EXPENSE"),
                "account_id": it.account_id,
                "quantity": float(it.quantity or 0),
                "unit_price": float(it.unit_price or 0),
                "unit_id": it.unit_id,
                "variant_id": it.variant_id,
                "warehouse_id": it.warehouse_id,
            }
            for it in src.items
        ],
    }


@bp.route("/<int:bill_id>")
@login_required
def view(bill_id):
    bill = db.session.get(VendorBill, bill_id)
    if not bill or bill.company_id != g.active_company.id or bill.deleted_at is not None:
        return redirect(url_for("vendor_bills.index"))
    payment_methods = PaymentMethod.query.filter_by(
        company_id=g.active_company.id, is_active=True
    ).all()
    return render_template("vendor_bills/view.html", bill=bill, payment_methods=payment_methods)


@bp.route("/<int:bill_id>/delete", methods=["POST"])
@login_required
@require_permission("vendor_bills.delete")
def delete(bill_id):
    """MARSOUD-52 + MARSOUD-VBILL-DELETE-POSTED (Abdelhamid 2026-07-15).

    DRAFT bills → soft delete (never posted, safe).
    Any other status → mirror the invoice-delete flow (commit 1ea029f):
      1. call post_vendor_bill_refund(FULL) which reverses AP, input
         VAT, restocks inventory + returns cash if it was paid.
      2. flip status to CANCELLED + mark deleted_at + deleted_by.
      3. audit trail preserved (bill row + refund row + journal
         entries stay in the DB).

    Same accounting invariants as the customer-invoice side:
      · vendor AP sub-account net balance = 0 after
      · Input VAT (1280) net balance = 0
      · Cash returns exactly the paid amount (for PAID bills)
      · A second delete on an already-CANCELLED bill is a no-op
    """
    bill = db.session.get(VendorBill, bill_id)
    if (not bill or bill.company_id != g.active_company.id
            or bill.deleted_at is not None):
        return redirect(url_for("vendor_bills.index"))

    reason = (request.form.get("reason") or "").strip() or "حذف الفاتورة"

    if bill.status == VendorBillStatus.DRAFT:
        # Never posted — safe to soft-delete.
        bill.deleted_at = datetime.utcnow()
        bill.deleted_by_id = current_user.id
        db.session.commit()
        flash(f"تم حذف الفاتورة {bill.number}", "success")
        return redirect(url_for("vendor_bills.index"))

    if bill.status == VendorBillStatus.CANCELLED:
        flash("الفاتورة ملغاة بالفعل", "warning")
        return redirect(url_for("vendor_bills.view", bill_id=bill.id))

    # Posted / paid / overdue — reverse via a FULL refund.
    from app.services.vendor_bills import (
        post_vendor_bill_refund,
    )
    try:
        post_vendor_bill_refund(
            bill, VendorRefundType.FULL, reason=reason,
            created_by=current_user.id,
        )
    except LedgerError as e:
        flash(str(e), "error")
        return redirect(url_for("vendor_bills.view", bill_id=bill.id))
    # Mark cancelled + soft-delete for full audit trail.
    bill.status = VendorBillStatus.CANCELLED
    bill.deleted_at = datetime.utcnow()
    bill.deleted_by_id = current_user.id
    db.session.commit()
    try:
        from app.services.superadmin import log_platform_action
        log_platform_action(
            "vendor_bill_deleted",
            target_company_id=bill.company_id,
            actor_id=current_user.id,
            details=f"#{bill.number} reason={reason[:60]}")
    except Exception:
        pass
    flash(f"تم حذف الفاتورة {bill.number} وعكس القيود المرتبطة بها",
          "success")
    return redirect(url_for("vendor_bills.index"))


# MARSOUD vendor-bill edit — supports full edit on DRAFT, cosmetic-only on posted
@bp.route("/<int:bill_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("vendor_bills.create")
def edit(bill_id):
    bill = db.session.get(VendorBill, bill_id)
    if not bill or bill.company_id != g.active_company.id:
        return redirect(url_for("vendor_bills.index"))

    full_edit = bill.status == VendorBillStatus.DRAFT
    vendors = Vendor.query.filter_by(
        company_id=g.active_company.id, is_active=True,
    ).order_by(Vendor.name).all()

    if request.method == "POST":
        try:
            if full_edit:
                # DRAFT — every field is fair game
                _populate_from_form(bill, request.form)
                db.session.commit()
            else:
                # Posted (or further) — only cosmetic fields. Item amounts/
                # accounts stay frozen so the journal entry remains valid.
                new_vendor_id = request.form.get("vendor_id") or None
                bill.vendor_id = int(new_vendor_id) if new_vendor_id else None
                bill.supplier_invoice_number = (
                    request.form.get("supplier_invoice_number") or ""
                ).strip()
                bill.notes = (request.form.get("notes") or "").strip()
                # MARSOUD-VENDOR-SUBCAT-BACKFILL (Abdelhamid 2026-07-15) —
                # per-item description + sub-category are safe to edit
                # even on a posted bill: neither touches the ledger.
                # Sub-category is validated against the bill's vendor
                # so a hand-crafted POST can't attach a foreign
                # vendor's taxonomy value.
                from app.models import VendorSubCategory
                for item in bill.items:
                    new_desc = request.form.get(f"item_desc_{item.id}")
                    if new_desc is not None:
                        item.description = new_desc.strip()
                    new_sc = request.form.get(f"item_subcat_{item.id}")
                    if new_sc is not None:
                        raw = new_sc.strip()
                        if not raw:
                            item.sub_category_id = None
                        elif bill.vendor_id:
                            row = VendorSubCategory.query.filter_by(
                                id=int(raw) if raw.isdigit() else -1,
                                vendor_id=bill.vendor_id,
                                company_id=bill.company_id,
                            ).first()
                            item.sub_category_id = row.id if row else None
                db.session.commit()
            flash("تم حفظ التعديلات", "success")
            return redirect(url_for("vendor_bills.view", bill_id=bill.id))
        except (LedgerError, ValueError) as e:
            db.session.rollback()
            flash(str(e), "error")

    return render_template(
        "vendor_bills/edit.html",
        bill=bill, vendors=vendors, full_edit=full_edit,
    )


@bp.route("/<int:bill_id>/post", methods=["POST"])
@login_required
@require_permission("vendor_bills.create")
def post(bill_id):
    bill = db.session.get(VendorBill, bill_id)
    if not bill or bill.company_id != g.active_company.id:
        return redirect(url_for("vendor_bills.index"))
    try:
        post_vendor_bill(bill, created_by=current_user.id)
        flash("تم تسجيل الفاتورة والقيد المحاسبي", "success")
    except LedgerError as e:
        flash(str(e), "error")
    return redirect(url_for("vendor_bills.view", bill_id=bill_id))


@bp.route("/<int:bill_id>/refund", methods=["POST"])
@login_required
@require_permission("vendor_bills.refund")
def refund(bill_id):
    """MARSOUD-REFUNDS-01 — issue a refund against a posted vendor bill."""
    bill = db.session.get(VendorBill, bill_id)
    if not bill or bill.company_id != g.active_company.id:
        return redirect(url_for("vendor_bills.index"))
    try:
        rtype = VendorRefundType[request.form.get("type")]
        raw_amount = request.form.get("amount")
        amount = float(raw_amount) if raw_amount else None
        reason = (request.form.get("reason") or "").strip() or None
        vbr = post_vendor_bill_refund(
            bill, rtype, amount=amount, reason=reason,
            created_by=current_user.id,
        )
        flash(f"تم تسجيل المرتجع {vbr.number}", "success")
    except (LedgerError, ValueError, KeyError) as e:
        flash(str(e) or "طلب مرتجع غير صالح", "error")
    return redirect(url_for("vendor_bills.view", bill_id=bill_id))


@bp.route("/<int:bill_id>/pay", methods=["POST"])
@login_required
@require_permission("vendor_bills.create")
def pay(bill_id):
    bill = db.session.get(VendorBill, bill_id)
    if not bill or bill.company_id != g.active_company.id:
        return redirect(url_for("vendor_bills.index"))
    try:
        amount = float(request.form.get("amount", 0))
        pmid = request.form.get("payment_method_id") or None
        record_bill_payment(bill, amount, payment_method_id=int(pmid) if pmid else None,
                           created_by=current_user.id)
        flash(f"تم تسجيل دفعة {amount:.2f}", "success")
    except (LedgerError, ValueError) as e:
        flash(str(e), "error")
    return redirect(url_for("vendor_bills.view", bill_id=bill_id))
