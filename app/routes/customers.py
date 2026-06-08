from flask import Blueprint, render_template, redirect, url_for, flash, request, g
from flask_login import login_required
from app import db
from app.models import Customer
from app.services.reports import aging_report
from app.services.permissions import require_permission

bp = Blueprint("customers", __name__)


@bp.route("/")
@login_required
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    customers = Customer.query.filter_by(company_id=g.active_company.id).order_by(Customer.name).all()
    return render_template("customers/index.html", customers=customers)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("partners.manage")
def new():
    if request.method == "POST":
        c = Customer(
            company_id=g.active_company.id,
            name=request.form.get("name", "").strip(),
            email=request.form.get("email", "").strip(),
            phone=request.form.get("phone", "").strip(),
            address=request.form.get("address", "").strip(),
            tax_number=request.form.get("tax_number", "").strip(),
        )
        if not c.name:
            flash("الاسم مطلوب", "error")
            return render_template("customers/form.html")
        db.session.add(c)
        db.session.commit()
        flash("تم إضافة العميل", "success")
        return redirect(url_for("customers.index"))
    return render_template("customers/form.html")


@bp.route("/<int:customer_id>")
@login_required
def view(customer_id):
    c = db.session.get(Customer, customer_id)
    if not c or c.company_id != g.active_company.id:
        return redirect(url_for("customers.index"))
    # FR-16 — list every project for this customer
    from app.models import Project
    customer_projects = Project.query.filter_by(
        customer_id=c.id,
    ).order_by(Project.created_at.desc()).all()
    return render_template("customers/view.html", customer=c,
                           customer_projects=customer_projects)


@bp.route("/aging")
@login_required
def aging():
    report = aging_report(g.active_company.id)
    return render_template("customers/aging.html", report=report)
