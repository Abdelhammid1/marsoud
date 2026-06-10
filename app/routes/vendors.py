from flask import Blueprint, render_template, redirect, url_for, flash, request, g
from flask_login import login_required
from app import db
from app.models import Vendor, VendorBill
from app.services.permissions import require_permission

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
        db.session.commit()
        flash("تم إضافة المورد", "success")
        return redirect(url_for("vendors.index"))
    return render_template("vendors/form.html")


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
