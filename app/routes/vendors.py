from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g,
    jsonify,
)
from flask_login import login_required, current_user
from app import db
from app.models import (
    Vendor, VendorBill, VendorBillRefund, DebitNote, VendorSubCategory,
)
from app.services.permissions import require_permission
from app.services.vendor_sub_categories import (
    SubCategoryError,
    create_sub_category, rename_sub_category, set_active,
    delete_sub_category, list_for_vendor, is_in_use,
)

bp = Blueprint("vendors", __name__)


def _vendor_or_404(vendor_id):
    v = db.session.get(Vendor, vendor_id)
    if not v or v.company_id != g.active_company.id:
        flash("المورد غير موجود", "error")
        return None
    return v


@bp.route("/")
@login_required
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    vendors = Vendor.query.filter_by(company_id=g.active_company.id).order_by(Vendor.name).all()
    return render_template("vendors/index.html", vendors=vendors)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("partners.manage")
def new():
    if request.method == "POST":
        v = Vendor(
            company_id=g.active_company.id,
            name=request.form.get("name", "").strip(),
            email=request.form.get("email", "").strip(),
            phone=request.form.get("phone", "").strip(),
            address=request.form.get("address", "").strip(),
            bank_account=request.form.get("bank_account", "").strip(),
            tax_number=request.form.get("tax_number", "").strip(),
        )
        if not v.name:
            flash("الاسم مطلوب", "error")
            return render_template("vendors/form.html")
        db.session.add(v)
        db.session.flush()
        # MARSOUD-COA-REBUILD — open a sub-account under 2110 at create
        # time so AP postings have a real leaf to land on.
        try:
            from app.services.subsidiary import ensure_vendor_account
            ensure_vendor_account(v)
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            flash(f"تعذّر إنشاء الحساب الفرعي للمورد: {e}", "error")
            return render_template("vendors/form.html")

        # MARSOUD-PARTY-OPENING-BALANCE-01 — same one-shot pattern as
        # the customer form. Only applied at create time.
        ob_raw = request.form.get("opening_balance")
        if ob_raw:
            try:
                ob_amount = float(ob_raw)
            except ValueError:
                ob_amount = 0.0
            if abs(ob_amount) > 0.001:
                from app.services.subsidiary import (
                    record_vendor_opening_balance,
                )
                from app.services.ledger import LedgerError
                try:
                    record_vendor_opening_balance(
                        v, ob_amount,
                        created_by=current_user.id if current_user.is_authenticated else None,
                    )
                except LedgerError as e:
                    db.session.rollback()
                    flash(str(e), "error")
                    return render_template("vendors/form.html")

        db.session.commit()
        flash("تم إضافة المورد", "success")
        return redirect(url_for("vendors.index"))
    return render_template("vendors/form.html")


@bp.route("/<int:vendor_id>")
@login_required
def view(vendor_id):
    """MARSOUD-REFUNDS-01 — minimal vendor detail page so refunds have
    somewhere to live (customer side already had one). Shows the vendor
    header, bills, and refunds/debit-notes."""
    v = _vendor_or_404(vendor_id)
    if not v:
        return redirect(url_for("vendors.index"))
    bills = VendorBill.query.filter_by(vendor_id=v.id).order_by(
        VendorBill.issue_date.desc(),
    ).all()
    refunds = db.session.query(VendorBillRefund, VendorBill).join(
        VendorBill, VendorBillRefund.bill_id == VendorBill.id,
    ).filter(VendorBill.vendor_id == v.id).order_by(
        VendorBillRefund.created_at.desc(),
    ).all()
    refunds_total = sum(float(r.amount or 0) for r, _ in refunds)
    debit_notes = DebitNote.query.filter_by(vendor_id=v.id).order_by(
        DebitNote.created_at.desc(),
    ).all()
    open_dn_balance = sum(dn.balance for dn in debit_notes)
    from app.models import PartyOpeningBalance, PartyType
    opening = PartyOpeningBalance.query.filter_by(
        company_id=v.company_id,
        party_type=PartyType.VENDOR, party_id=v.id,
    ).first()
    return render_template(
        "vendors/view.html", vendor=v, bills=bills,
        refunds=refunds, refunds_total=refunds_total,
        debit_notes=debit_notes, open_dn_balance=open_dn_balance,
        opening_balance=opening,
    )


# MARSOUD-29 — edit existing vendor
@bp.route("/<int:vendor_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("partners.manage")
def edit(vendor_id):
    v = _vendor_or_404(vendor_id)
    if not v:
        return redirect(url_for("vendors.index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("الاسم مطلوب", "error")
            return render_template("vendors/form.html", vendor=v)
        v.name = name
        v.email = request.form.get("email", "").strip()
        v.phone = request.form.get("phone", "").strip()
        v.address = request.form.get("address", "").strip()
        v.bank_account = request.form.get("bank_account", "").strip()
        v.tax_number = request.form.get("tax_number", "").strip()
        db.session.commit()
        flash("تم حفظ التعديلات", "success")
        return redirect(url_for("vendors.index"))
    return render_template("vendors/form.html", vendor=v)


# MARSOUD-29 — delete vendor (with integrity guard)
@bp.route("/<int:vendor_id>/delete", methods=["POST"])
@login_required
@require_permission("partners.manage")
def delete(vendor_id):
    v = _vendor_or_404(vendor_id)
    if not v:
        return redirect(url_for("vendors.index"))
    # Refuse delete if there are bills linked — would orphan ledger references
    bill_count = VendorBill.query.filter_by(vendor_id=v.id).count()
    if bill_count > 0:
        # Soft-deactivate instead of hard delete
        v.is_active = False
        db.session.commit()
        flash(
            f"تم أرشفة المورد لأن عليه {bill_count} فاتورة — "
            f"لا يمكن حذفه نهائياً للحفاظ على القيود.",
            "warning",
        )
    else:
        db.session.delete(v)
        db.session.commit()
        flash("تم حذف المورد", "success")
    return redirect(url_for("vendors.index"))


# ─── MARSOUD-VENDOR-SUBCAT (Abdelhamid 2026-07-14) ──────────────────────
# Per-vendor sub-category management. Lives under the vendor detail
# URL so the taxonomy is visibly nested inside the vendor it belongs
# to. All mutations go through the service layer to keep the
# uniqueness + delete-guard rules in one place.
@bp.route("/<int:vendor_id>/sub-categories")
@login_required
@require_permission("partners.manage")
def sub_categories_index(vendor_id):
    v = _vendor_or_404(vendor_id)
    if not v:
        return redirect(url_for("vendors.index"))
    rows = list_for_vendor(v.id, active_only=False)
    # Tag each row with `is_used` so the template can show "in use →
    # deactivate only" hints without an extra service call.
    for r in rows:
        r._is_used = is_in_use(r)
    return render_template(
        "vendors/sub_categories.html", vendor=v, sub_categories=rows,
    )


@bp.route("/<int:vendor_id>/sub-categories/new", methods=["POST"])
@login_required
@require_permission("partners.manage")
def sub_categories_create(vendor_id):
    v = _vendor_or_404(vendor_id)
    if not v:
        return redirect(url_for("vendors.index"))
    try:
        create_sub_category(
            company_id=v.company_id, vendor_id=v.id,
            name=request.form.get("name"),
            created_by_id=current_user.id,
        )
        flash("تم إضافة التصنيف", "success")
    except SubCategoryError as e:
        flash(str(e), "error")
    return redirect(url_for(
        "vendors.sub_categories_index", vendor_id=v.id))


@bp.route("/<int:vendor_id>/sub-categories/<int:sc_id>/edit",
           methods=["POST"])
@login_required
@require_permission("partners.manage")
def sub_categories_edit(vendor_id, sc_id):
    v = _vendor_or_404(vendor_id)
    if not v:
        return redirect(url_for("vendors.index"))
    sc = VendorSubCategory.query.filter_by(
        id=sc_id, vendor_id=v.id, company_id=v.company_id,
    ).first()
    if not sc:
        flash("التصنيف غير موجود", "error")
        return redirect(url_for(
            "vendors.sub_categories_index", vendor_id=v.id))
    try:
        rename_sub_category(sc, name=request.form.get("name"))
        flash("تم تحديث التصنيف", "success")
    except SubCategoryError as e:
        flash(str(e), "error")
    return redirect(url_for(
        "vendors.sub_categories_index", vendor_id=v.id))


@bp.route("/<int:vendor_id>/sub-categories/<int:sc_id>/toggle",
           methods=["POST"])
@login_required
@require_permission("partners.manage")
def sub_categories_toggle(vendor_id, sc_id):
    v = _vendor_or_404(vendor_id)
    if not v:
        return redirect(url_for("vendors.index"))
    sc = VendorSubCategory.query.filter_by(
        id=sc_id, vendor_id=v.id, company_id=v.company_id,
    ).first()
    if sc:
        set_active(sc, not sc.is_active)
        flash("تم تحديث حالة التصنيف", "success")
    return redirect(url_for(
        "vendors.sub_categories_index", vendor_id=v.id))


@bp.route("/<int:vendor_id>/sub-categories/<int:sc_id>/delete",
           methods=["POST"])
@login_required
@require_permission("partners.manage")
def sub_categories_delete(vendor_id, sc_id):
    v = _vendor_or_404(vendor_id)
    if not v:
        return redirect(url_for("vendors.index"))
    sc = VendorSubCategory.query.filter_by(
        id=sc_id, vendor_id=v.id, company_id=v.company_id,
    ).first()
    if not sc:
        flash("التصنيف غير موجود", "error")
        return redirect(url_for(
            "vendors.sub_categories_index", vendor_id=v.id))
    try:
        delete_sub_category(sc)
        flash("تم حذف التصنيف", "success")
    except SubCategoryError as e:
        flash(str(e), "error")
    return redirect(url_for(
        "vendors.sub_categories_index", vendor_id=v.id))


@bp.route("/<int:vendor_id>/sub-categories.json")
@login_required
def sub_categories_api(vendor_id):
    """JSON feed for the bill-form dropdown — active categories only.
    Scoped to the current company at the query level so a hand-crafted
    vendor_id from a different company returns an empty list."""
    v = db.session.get(Vendor, vendor_id)
    if not v or v.company_id != g.active_company.id:
        return jsonify([])
    rows = list_for_vendor(v.id, active_only=True)
    return jsonify([
        {"id": r.id, "name": r.name} for r in rows
    ])
