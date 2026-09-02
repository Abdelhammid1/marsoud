"""MARSOUD-ERP-01 Phase 2 — POS register routes."""
import json
from datetime import datetime, date, timedelta
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g, abort,
    jsonify,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    Invoice, InvoiceStatus, InvoiceItem, ProductVariant, Product,
    PaymentMethod, Customer, Warehouse, JournalEntry, User,
)
from app.models.user import user_companies
from app.services.permissions import require_permission, has_permission
from app.services.pos import create_pos_order, void_pos_order, POSError
from app.services.inventory import (
    find_variant_by_barcode, default_warehouse,
)
from app.services.ledger import LedgerError


bp = Blueprint("pos", __name__)


# ─── Register screen ─────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("pos.use")
def index():
    cid = g.active_company.id
    # ERP-03 — shift gate. When the company requires shifts and the
    # cashier doesn't have one open, redirect to the open-shift form.
    from app.services.pos_shifts import (
        shift_required, current_open_shift_for,
    )
    if shift_required(cid):
        open_shift = current_open_shift_for(current_user.id, cid)
        if not open_shift:
            flash(
                "افتح وردية قبل البدء بالبيع.",
                "warning",
            )
            return redirect(url_for("pos.shift_open_form"))
    pms = PaymentMethod.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(PaymentMethod.is_default.desc(), PaymentMethod.name).all()
    customers = Customer.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(Customer.name).limit(200).all()
    # MARSOUD-PRODUCT-HIERARCHY-01 — categories for the category-tabs
    # picker. Each product's default variant carries the sellable SKU.
    from app.models import ProductCategory, Product, ProductVariant
    # MARSOUD-CATEGORY-VISIBILITY-01 — a category switched off for POS
    # loses its tab as well as its products. Leaving the tab would open
    # an empty panel, which reads as a bug rather than as a setting.
    from app.services.category_visibility import (
        visible_categories, product_visible_clause,
    )
    categories = visible_categories(cid, "pos")
    # Pre-compute per-category product cards so the picker renders
    # without additional queries. Only tracked products with an active
    # default variant show up (services can't be scanned).
    products_by_cat = {}
    tracked_q = Product.query.filter_by(
        company_id=cid, is_active=True, is_tracked=True,
    )
    _vis = product_visible_clause(cid, "pos")
    if _vis is not None:
        tracked_q = tracked_q.filter(_vis)
    tracked = tracked_q.all()
    for p in tracked:
        v = p.default_variant
        if not v or not v.is_active:
            continue
        # MARSOUD-UNIT-CONVERSION-01 — bundle every unit the cashier
        # can pick from. UI defaults to the base unit; picker dropdown
        # exposes any extras (كرتونة, طبق, ...).
        # MARSOUD-UOM-PRICE — each unit carries its own sale_price so
        # switching the unit in the cart auto-updates the line price.
        units = [
            {"id": u.id, "name": u.unit_name,
             "factor": float(u.conversion_factor or 1),
             "is_base": bool(u.is_base),
             "sale_price": float(u.effective_sale_price)}
            for u in p.units
        ]
        # Fallback tax rate: use the company's configured VAT (0 if
        # unset) — NOT a hardcoded 15. Some tenants are in Egypt (14),
        # some in KSA (15), some VAT-exempt (0).
        company_vat_rate = float(g.active_company.vat_rate or 0)
        products_by_cat.setdefault(p.category_id or 0, []).append({
            "variant_id": v.id, "sku": v.sku,
            "product_id": p.id, "name": p.name,
            "price": float(p.default_price or 0),
            "tax_rate": (
                float(p.default_tax_rate)
                if p.default_tax_rate is not None else company_vat_rate
            ),
            "units": units,
            # MARSOUD-DUAL-UOM-WEIGHT-01 pt 2 (Abdelhamid 2026-07-25)
            "tracks_piece_count": bool(
                getattr(p, "tracks_piece_count", False)),
        })
    return render_template(
        "pos/register.html",
        payment_methods=pms,
        customers=customers,
        default_warehouse=default_warehouse(cid),
        categories=categories,
        products_by_cat=products_by_cat,
        company_vat_rate=float(g.active_company.vat_rate or 0),
    )


# ─── Shifts (ERP-03) ─────────────────────────────────────────────────────
@bp.route("/shifts/")
@login_required
@require_permission("shifts.manage")
def shifts():
    cid = g.active_company.id
    from app.models import CashierShift
    show_all = request.args.get("all") == "1"
    q = CashierShift.query.filter_by(company_id=cid)
    if not show_all or not has_permission("users.view"):
        q = q.filter(CashierShift.cashier_id == current_user.id)
    rows = q.order_by(CashierShift.opened_at.desc()).limit(50).all()
    return render_template("pos/shifts_index.html", shifts=rows,
                           show_all=show_all)


@bp.route("/shifts/open", methods=["GET", "POST"])
@login_required
@require_permission("shifts.manage")
def shift_open_form():
    cid = g.active_company.id
    from app.services.pos_shifts import (
        open_shift, current_open_shift_for, ShiftError,
    )
    # If already has an open shift, show that instead of letting them open
    # another.
    existing = current_open_shift_for(current_user.id, cid)
    if existing:
        return redirect(url_for("pos.shift_detail", shift_id=existing.id))
    if request.method == "POST":
        try:
            sh = open_shift(
                company_id=cid, cashier_id=current_user.id,
                opening_cash=float(request.form.get("opening_cash") or 0),
                notes=request.form.get("notes"),
            )
            flash(f"تم فتح الوردية #{sh.id}", "success")
            return redirect(url_for("pos.index"))
        except (ShiftError, ValueError) as e:
            flash(str(e), "error")
    return render_template("pos/shift_open_form.html")


@bp.route("/shifts/<int:shift_id>/close", methods=["GET", "POST"])
@login_required
@require_permission("shifts.manage")
def shift_close(shift_id):
    from app.models import CashierShift
    from app.services.pos_shifts import close_shift, ShiftError
    sh = db.session.get(CashierShift, shift_id)
    if not sh or sh.company_id != g.active_company.id:
        abort(404)
    if sh.cashier_id != current_user.id and not has_permission("users.view"):
        abort(403)
    if sh.status == "CLOSED":
        flash("الوردية مقفلة بالفعل", "warning")
        return redirect(url_for("pos.shift_detail", shift_id=sh.id))
    if request.method == "POST":
        try:
            close_shift(sh,
                        closing_cash=float(request.form.get("closing_cash") or 0),
                        notes=request.form.get("notes"))
            flash(
                f"تم إغلاق الوردية — فرق الدرج: {float(sh.variance or 0):.2f}",
                "success" if abs(float(sh.variance or 0)) < 0.01 else "warning",
            )
            return redirect(url_for("pos.shift_detail", shift_id=sh.id))
        except (ShiftError, ValueError) as e:
            flash(str(e), "error")
    # Pre-compute expected so the form can show it.
    from app.services.pos_shifts import _expected_cash_for
    expected = _expected_cash_for(sh)
    return render_template("pos/shift_close_form.html",
                           shift=sh, expected=expected)


@bp.route("/shifts/<int:shift_id>")
@login_required
@require_permission("shifts.manage")
def shift_detail(shift_id):
    from app.models import CashierShift
    from app.services.pos_shifts import shift_summary
    sh = db.session.get(CashierShift, shift_id)
    if not sh or sh.company_id != g.active_company.id:
        abort(404)
    if sh.cashier_id != current_user.id and not has_permission("users.view"):
        abort(403)
    return render_template("pos/shift_detail.html",
                           shift=sh, summary=shift_summary(sh))


# ─── JSON lookup for the barcode scanner ─────────────────────────────────
@bp.route("/lookup", methods=["POST"])
@login_required
@require_permission("pos.use")
def lookup():
    cid = g.active_company.id
    q = (request.json.get("query") if request.is_json
         else request.form.get("query") or "").strip()
    if not q:
        return jsonify({"error": "أدخل باركود أو SKU"}), 400
    v = find_variant_by_barcode(cid, q)
    if not v:
        v = ProductVariant.query.filter_by(
            company_id=cid, sku=q, is_active=True,
        ).first()
    # MARSOUD-CATEGORY-VISIBILITY-01 — the hole the ticket names: hiding a
    # category from the GRID is worthless if the same product is still
    # scannable. Both branches above resolve a variant without ever
    # touching Product, so the check goes here, after resolution, and
    # answers with the same 404 an unknown barcode gets — a cashier
    # should not be able to tell "hidden" from "does not exist".
    #
    # Deliberately NOT inside find_variant_by_barcode
    # (services/inventory.py): the AI agent shares that helper and is out
    # of scope for this ticket.
    from app.services.category_visibility import is_product_visible
    if v and not is_product_visible(v.product, "pos"):
        v = None
    if not v:
        return jsonify({"error": f"الباركود/SKU '{q}' غير معروف"}), 404
    # MARSOUD-PRODUCT-BUNDLES-01 — pre-flight bundle availability. If
    # this variant belongs to a bundle product, check component stock
    # for a qty of 1 and surface the result so the cart can warn the
    # cashier BEFORE they scan through to payment.
    is_bundle = bool(getattr(v.product, "is_bundle", False))
    bundle_ok = True
    bundle_msg = None
    if is_bundle:
        from app.services.inventory import default_warehouse
        from app.services.bundles import check_bundle_availability
        wh = default_warehouse(g.active_company.id)
        if wh:
            bundle_ok, bundle_msg = check_bundle_availability(
                v.product, 1, wh)

    return jsonify({
        "variant_id": v.id,
        "sku": v.sku,
        "name": v.display_name,
        "price": float(v.product.default_price or 0),
        "stock": v.total_qty,
        "tax_rate": float(
            v.product.default_tax_rate
            if v.product.default_tax_rate is not None
            else (g.active_company.vat_rate or 0)
        ),
        # MARSOUD-DUAL-UOM-WEIGHT-01 pt 2 (Abdelhamid 2026-07-25) —
        # flag so the cart can show a piece-count input.
        "tracks_piece_count": bool(
            getattr(v.product, "tracks_piece_count", False)),
        # MARSOUD-PRODUCT-BUNDLES-01 — bundle metadata for the cart UI.
        "is_bundle": is_bundle,
        "bundle_ok": bundle_ok,
        "bundle_msg": bundle_msg,
    })


# ─── Submit order ────────────────────────────────────────────────────────
@bp.route("/orders/new", methods=["POST"])
@login_required
@require_permission("pos.use")
def order_new():
    cid = g.active_company.id
    try:
        # The register page submits a JSON-encoded cart in 'cart_json' so
        # we can carry the per-line discount info without proliferating
        # form fields.
        raw_cart = request.form.get("cart_json") or "[]"
        cart = json.loads(raw_cart)
        if not isinstance(cart, list) or not cart:
            raise POSError("الكارت فارغ")

        pm_id = int(request.form.get("payment_method_id"))
        cust_raw = (request.form.get("customer_id") or "").strip()
        customer_id = int(cust_raw) if cust_raw else None
        cash_raw = (request.form.get("cash_received") or "").strip()
        cash_received = float(cash_raw) if cash_raw else None
        tax_rate_raw = (request.form.get("tax_rate") or "").strip()
        tax_rate = float(tax_rate_raw) if tax_rate_raw else None

        invoice = create_pos_order(
            company_id=cid, items=cart, payment_method_id=pm_id,
            cashier_id=current_user.id,
            customer_id=customer_id, cash_received=cash_received,
            tax_rate=tax_rate,
        )
        flash(f"تم تسجيل الأوردر {invoice.number}", "success")
        return redirect(url_for("pos.receipt", number=invoice.number))
    except (POSError, LedgerError, ValueError, TypeError, KeyError) as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("pos.index"))


# MARSOUD-POS-URL-OPACITY (Abdelhamid 2026-07-19) — receipt / detail /
# void routes now key on the per-company Invoice.number (e.g. POS-0001)
# instead of the global auto-increment id. Two goals:
#   1. Customer sees /pos/orders/POS-0001/receipt in the address bar,
#      not /pos/orders/81/receipt. The old id leaked the system-wide
#      count of POS orders across every tenant on the install.
#   2. Number is scoped per-company + already used on receipts, so we
#      pick it up "for free" instead of adding a new column.
# `<path:number>` because our numbering format contains a hyphen and
# Flask's default int/string converters would either reject it or
# match too greedily. `path` accepts everything up to the next slash.
# ─── Receipt (print) ─────────────────────────────────────────────────────
@bp.route("/orders/<path:number>/receipt")
@login_required
@require_permission("pos.use")
def receipt(number):
    cid = g.active_company.id
    inv = _order_or_404(number, cid)
    return render_template("pos/receipt.html", invoice=inv,
                           company=g.active_company)


# ─── Detail ──────────────────────────────────────────────────────────────
@bp.route("/orders/<path:number>")
@login_required
@require_permission("pos.use")
def detail(number):
    cid = g.active_company.id
    inv = _order_or_404(number, cid)
    return render_template("pos/detail.html", invoice=inv,
                           can_void=has_permission("pos.void")
                                     and not inv.is_voided)


# ─── Void ────────────────────────────────────────────────────────────────
@bp.route("/orders/<path:number>/void", methods=["POST"])
@login_required
@require_permission("pos.void")
def void(number):
    cid = g.active_company.id
    inv = _order_or_404(number, cid)
    reason = (request.form.get("reason") or "").strip()
    try:
        void_pos_order(inv, reason=reason, actor_id=current_user.id)
        flash("تم إلغاء الأوردر وإرجاع المخزون", "success")
        return redirect(url_for("pos.detail", number=inv.number))
    except POSError as e:
        flash(str(e), "error")
        return redirect(url_for("pos.detail", number=inv.number))


# ─── History (cashier's own orders) ──────────────────────────────────────
@bp.route("/history/")
@login_required
@require_permission("pos.use")
def history():
    cid = g.active_company.id
    # Mine by default; owners/admins can see all if ?all=1.
    show_all = request.args.get("all") == "1"
    q = Invoice.query.filter_by(company_id=cid, source="POS")
    if not show_all or not has_permission("users.view"):
        q = q.filter(Invoice.cashier_id == current_user.id)
    rows = q.order_by(Invoice.created_at.desc()).limit(100).all()
    return render_template("pos/history.html",
                           orders=rows, show_all=show_all)


# ─── Helpers ─────────────────────────────────────────────────────────────
def _order_or_404(number_or_id, company_id):
    """Look up a POS invoice by its per-company number (POS-0001) OR
    by its numeric id — the latter kept for backward-compat with any
    external bookmark / integration that still uses the id.
    Always company-scoped."""
    inv = Invoice.query.filter_by(
        company_id=company_id, number=str(number_or_id), source="POS",
    ).first()
    if not inv:
        # Fallback: legacy numeric-id URL. Still company-scoped so we
        # never leak another tenant's invoice.
        try:
            _id = int(number_or_id)
        except (TypeError, ValueError):
            _id = None
        if _id is not None:
            inv = db.session.get(Invoice, _id)
            if inv and (inv.company_id != company_id
                        or inv.source != "POS"):
                inv = None
    if not inv:
        abort(404)
    return inv
