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


@bp.route("/")
@login_required
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    update_overdue_vendor_bills(g.active_company.id)

    q = VendorBill.query.filter_by(company_id=g.active_company.id).filter(VendorBill.deleted_at.is_(None))
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

    total_invoiced = sum(float(b.total or 0) for b in bills)
    total_paid = sum(float(b.paid_amount or 0) for b in bills)
    total_outstanding = sum(b.balance for b in bills if b.status not in (VendorBillStatus.CANCELLED,))

    totals = {
        "count": len(bills),
        "invoiced": total_invoiced,
        "paid": total_paid,
        "outstanding": total_outstanding,
    }
    return render_template("vendor_bills/index.html", bills=bills, statuses=VendorBillStatus, totals=totals)


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


def _resolve_inventory_target(form, i, company_id):
    """For row i of an INVENTORY line, return (variant, warehouse).
    Creates a new Product + ProductVariant inline when the row supplies
    new_product_name + new_product_sku instead of a variant_id."""
    variants_in = form.getlist("item_variant_id[]")
    warehouses_in = form.getlist("item_warehouse_id[]")
    new_names = form.getlist("item_new_product_name[]")
    new_skus = form.getlist("item_new_product_sku[]")

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
        )
        db.session.add(p)
        db.session.flush()
        v = ProductVariant(
            company_id=company_id, product_id=p.id,
            sku=new_sku, name="",
        )
        db.session.add(v)
        db.session.flush()
        return v, wh

    # Otherwise: existing variant
    v_id_raw = variants_in[i] if i < len(variants_in) else ""
    if not v_id_raw:
        raise LedgerError("سطر مخزون يحتاج اختيار variant أو إنشاء منتج جديد")
    v = db.session.get(ProductVariant, int(v_id_raw))
    if not v or v.company_id != company_id:
        raise LedgerError("الـ variant غير صحيح")
    return v, wh


def _populate_from_form(bill, form):
    """Fill bill + items from form data."""
    bill.vendor_id = int(form.get("vendor_id")) if form.get("vendor_id") else None
    bill.supplier_invoice_number = (form.get("supplier_invoice_number") or "").strip()
    bill.issue_date = datetime.strptime(form.get("issue_date") or date.today().isoformat(), "%Y-%m-%d").date()
    bill.due_date = datetime.strptime(form.get("due_date") or (date.today() + timedelta(days=30)).isoformat(), "%Y-%m-%d").date()
    bill.payment_method = VendorBillPaymentMethod[form.get("payment_method", "CASH")]
    bill.notes = form.get("notes", "")
    bill.tax_rate = float(form.get("tax_rate", 0) or 0)

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

    for i, desc in enumerate(descriptions):
        if not (desc or "").strip():
            continue
        lt = BillLineType[types[i] if i < len(types) else "EXPENSE"]
        uid_raw = unit_ids[i] if i < len(unit_ids) else None
        try:
            uid = int(uid_raw) if uid_raw else None
        except (TypeError, ValueError):
            uid = None
        item = VendorBillItem(
            bill_id=bill.id,
            description=desc.strip(),
            line_type=lt,
            account_id=int(accounts[i]),
            quantity=float(quantities[i] or 1),
            unit_price=float(prices[i] or 0),
            unit_id=uid,
        )
        if lt == BillLineType.FIXED_ASSET:
            item.useful_life_years = int(lives[i] or 0) if i < len(lives) and lives[i] else None
            item.salvage_value = float(salvages[i] or 0) if i < len(salvages) and salvages[i] else 0
        elif lt == BillLineType.INVENTORY:
            # MARSOUD-50.3 — resolve variant + warehouse for INVENTORY lines.
            # Inline-creates a Product+Variant if the row supplied a new name.
            v, wh = _resolve_inventory_target(form, i, bill.company_id)
            item.variant_id = v.id
            item.warehouse_id = wh.id
        db.session.add(item)
    db.session.flush()
    bill.items = VendorBillItem.query.filter_by(bill_id=bill.id).all()
    bill.recalc()


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("vendor_bills.create")
def new():
    if not g.active_company:
        return redirect(url_for("companies.new"))
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

    # MARSOUD-RECURRING-PAY — support ?from_recurring=<id> to prefill the
    # form from a recurring-bill template. The user still has to click
    # save; nothing is written until they do. Date defaults to today
    # (the JS init at the bottom of the template) so the new occurrence
    # is dated correctly out of the box.
    prefill = _prefill_from_recurring(
        request.args.get("from_recurring", type=int))
    return render_template(
        "vendor_bills/form.html", bill=None, vendors=vendors,
        prefill=prefill,
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
    """MARSOUD-52 — soft delete a DRAFT vendor bill. OWNER + admin only.
    Refused for any non-DRAFT status (posted bills have a journal entry +
    possibly stock movements / payments that must not be silently dropped)."""
    bill = db.session.get(VendorBill, bill_id)
    if not bill or bill.company_id != g.active_company.id or bill.deleted_at is not None:
        return redirect(url_for("vendor_bills.index"))
    if bill.status != VendorBillStatus.DRAFT:
        flash(
            f"لا يمكن حذف فاتورة {bill.number} — حالتها {bill.status.value} وليس DRAFT.",
            "error",
        )
        return redirect(url_for("vendor_bills.view", bill_id=bill.id))
    bill.deleted_at = datetime.utcnow()
    bill.deleted_by_id = current_user.id
    db.session.commit()
    flash(f"تم حذف الفاتورة {bill.number}", "success")
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
                # Per-item description edits (keyed by item id)
                for item in bill.items:
                    new_desc = request.form.get(f"item_desc_{item.id}")
                    if new_desc is not None:
                        item.description = new_desc.strip()
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
