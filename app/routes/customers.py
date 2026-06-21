from flask import Blueprint, render_template, redirect, url_for, flash, request, g, abort
from flask_login import login_required
from app import db
from app.models import Customer, User
from app.models.user import user_companies
from app.services.reports import aging_report
from app.services.permissions import require_permission

bp = Blueprint("customers", __name__)


def _company_reps():
    """Users in the active company eligible to be a customer's sales rep."""
    cid = g.active_company.id
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == cid)
    ).fetchall()
    out, seen = [], set()
    for r in rows:
        if r.user_id in seen:
            continue
        seen.add(r.user_id)
        u = db.session.get(User, r.user_id)
        if u and u.is_active and u.linked_customer_id is None:
            out.append(u)
    return out


def _parse_commission_rate(raw):
    """Accept blank → None, else a float in [0,100]."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    if v < 0 or v > 100:
        return None
    return v


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
    reps = _company_reps()
    if request.method == "POST":
        rep_raw = request.form.get("sales_rep_id")
        c = Customer(
            company_id=g.active_company.id,
            name=request.form.get("name", "").strip(),
            email=request.form.get("email", "").strip(),
            phone=request.form.get("phone", "").strip(),
            address=request.form.get("address", "").strip(),
            tax_number=request.form.get("tax_number", "").strip(),
            sales_rep_id=int(rep_raw) if rep_raw and rep_raw.isdigit() else None,
            commission_rate=_parse_commission_rate(request.form.get("commission_rate")),
        )
        if not c.name:
            flash("الاسم مطلوب", "error")
            return render_template("customers/form.html", customer=None, reps=reps)
        db.session.add(c)
        db.session.commit()
        flash("تم إضافة العميل", "success")
        return redirect(url_for("customers.index"))
    return render_template("customers/form.html", customer=None, reps=reps)


@bp.route("/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("partners.manage")
def edit(customer_id):
    c = db.session.get(Customer, customer_id)
    if not c or c.company_id != g.active_company.id:
        abort(404)
    reps = _company_reps()
    if request.method == "POST":
        rep_raw = request.form.get("sales_rep_id")
        c.name = request.form.get("name", c.name).strip()
        c.email = (request.form.get("email") or "").strip() or None
        c.phone = (request.form.get("phone") or "").strip() or None
        c.address = (request.form.get("address") or "").strip() or None
        c.tax_number = (request.form.get("tax_number") or "").strip() or None
        c.sales_rep_id = int(rep_raw) if rep_raw and rep_raw.isdigit() else None
        c.commission_rate = _parse_commission_rate(request.form.get("commission_rate"))
        db.session.commit()
        flash("تم حفظ التعديلات", "success")
        return redirect(url_for("customers.view", customer_id=c.id))
    return render_template("customers/form.html", customer=c, reps=reps)


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
