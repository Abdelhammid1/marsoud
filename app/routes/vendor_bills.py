from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, redirect, url_for, flash, request, g, jsonify
from flask_login import login_required, current_user
from app import db
from app.models import (
    VendorBill, VendorBillItem, VendorBillStatus, VendorBillPaymentMethod,
    BillLineType, Vendor, Account, PaymentMethod,
)
from app.services.vendor_bills import (
    post_vendor_bill, record_bill_payment, update_overdue_vendor_bills,
    get_allowed_accounts_for_line_type,
)
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

    q = VendorBill.query.filter_by(company_id=g.active_company.id)
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

    for i, desc in enumerate(descriptions):
        if not (desc or "").strip():
            continue
        lt = BillLineType[types[i] if i < len(types) else "EXPENSE"]
        item = VendorBillItem(
            bill_id=bill.id,
            description=desc.strip(),
            line_type=lt,
            account_id=int(accounts[i]),
            quantity=float(quantities[i] or 1),
            unit_price=float(prices[i] or 0),
        )
        if lt == BillLineType.FIXED_ASSET:
            item.useful_life_years = int(lives[i] or 0) if i < len(lives) and lives[i] else None
            item.salvage_value = float(salvages[i] or 0) if i < len(salvages) and salvages[i] else 0
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

    return render_template("vendor_bills/form.html", bill=None, vendors=vendors)


@bp.route("/<int:bill_id>")
@login_required
def view(bill_id):
    bill = db.session.get(VendorBill, bill_id)
    if not bill or bill.company_id != g.active_company.id:
        return redirect(url_for("vendor_bills.index"))
    payment_methods = PaymentMethod.query.filter_by(
        company_id=g.active_company.id, is_active=True
    ).all()
    return render_template("vendor_bills/view.html", bill=bill, payment_methods=payment_methods)


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
