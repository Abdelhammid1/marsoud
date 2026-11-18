from flask import Blueprint, render_template, redirect, url_for, flash, request, g
from flask_login import login_required
from app import db
from app.models import PaymentMethod, Account, Payment
from app.services.permissions import require_permission

bp = Blueprint("payment_methods", __name__)


@bp.route("/")
@login_required
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    methods = PaymentMethod.query.filter_by(company_id=g.active_company.id).all()
    return render_template("payment_methods/index.html", methods=methods)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("payment_methods.manage")
def new():
    # Show only Asset accounts as valid receiving accounts
    accounts = Account.query.filter_by(
        company_id=g.active_company.id, is_active=True
    ).filter(Account.code.like("11%")).order_by(Account.code).all()

    if request.method == "POST":
        try:
            name = request.form.get("name", "").strip()
            if not name:
                raise ValueError("الاسم مطلوب")
            if PaymentMethod.query.filter_by(company_id=g.active_company.id, name=name).first():
                raise ValueError("اسم مكرر")
            pm = PaymentMethod(
                company_id=g.active_company.id,
                name=name,
                name_ar=request.form.get("name_ar", "").strip(),
                account_id=int(request.form.get("account_id")),
            )
            db.session.add(pm)
            db.session.commit()
            flash("تم إضافة طريقة الدفع", "success")
            return redirect(url_for("payment_methods.index"))
        except (ValueError, KeyError) as e:
            flash(str(e), "error")
    return render_template("payment_methods/form.html", accounts=accounts)


@bp.route("/<hid:method_id>/toggle", methods=["POST"])
@login_required
@require_permission("payment_methods.manage")
def toggle(method_id):
    pm = db.session.get(PaymentMethod, method_id)
    if not pm or pm.company_id != g.active_company.id:
        return redirect(url_for("payment_methods.index"))
    has_payments = Payment.query.filter_by(payment_method_id=pm.id).first() is not None
    if not pm.is_active or not has_payments:
        # OK to flip; if no payments and turning off, soft-deactivate
        pm.is_active = not pm.is_active
    else:
        pm.is_active = not pm.is_active   # soft-deactivate even with payments — keep history
    db.session.commit()
    flash("تم التحديث", "success")
    return redirect(url_for("payment_methods.index"))
