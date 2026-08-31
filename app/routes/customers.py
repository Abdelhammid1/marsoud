from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g, abort,
    jsonify,
)
from flask_login import login_required, current_user
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
@require_permission("customers.view")
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
        # MARSOUD-TKT-ADMIN-OWNER-COL — persist contact_person (optional).
        c = Customer(
            company_id=g.active_company.id,
            name=request.form.get("name", "").strip(),
            contact_person=(request.form.get("contact_person") or "").strip() or None,
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
        db.session.flush()
        # MARSOUD-COA-REBUILD — open a sub-account under 1130 at create
        # time so the customer is visible in the trial balance from day 1.
        try:
            from app.services.subsidiary import ensure_customer_account
            ensure_customer_account(c)
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            flash(f"تعذّر إنشاء الحساب الفرعي للعميل: {e}", "error")
            return render_template("customers/form.html", customer=None, reps=reps)

        # MARSOUD-PARTY-OPENING-BALANCE-01 — optional opening balance
        # captured at create time only. Zero (default) means no journal.
        ob_raw = request.form.get("opening_balance")
        if ob_raw:
            try:
                ob_amount = float(ob_raw)
            except ValueError:
                ob_amount = 0.0
            if abs(ob_amount) > 0.001:
                from app.services.subsidiary import (
                    record_customer_opening_balance,
                )
                from app.services.ledger import LedgerError
                try:
                    record_customer_opening_balance(
                        c, ob_amount,
                        created_by=current_user.id if current_user.is_authenticated else None,
                    )
                except LedgerError as e:
                    db.session.rollback()
                    flash(str(e), "error")
                    return render_template("customers/form.html",
                                             customer=None, reps=reps)

        db.session.commit()
        # MARSOUD-METRIC-AUTOMATION (2026-08-05) — the metric job reads
        # the unified activity log and nothing else, so a new customer
        # has to leave a row here to be scorable at all.
        try:
            from app.services.activity import log_action
            log_action(action_type="CREATE", entity_type="customer",
                       entity_id=c.id, entity_label=c.name,
                       company_id=c.company_id)
        except Exception:
            pass
        flash("تم إضافة العميل", "success")
        return redirect(url_for("customers.index"))
    return render_template("customers/form.html", customer=None, reps=reps)


# MARSOUD-TKT-INVOICE-INLINE-CUSTOMER (Abdelhamid 2026-08-29) — JSON
# counterpart to `new`. Called from a modal on the invoice-new page so
# a bookkeeper can add a walk-in customer without losing their place
# in the invoice they're mid-way through drafting. Same permission
# gate (`partners.manage`), same server-side validation, same
# `ensure_customer_account` call, same activity log — the only
# differences from `new` are: (a) returns JSON instead of redirecting,
# (b) rolls back + returns 400 on validation error instead of
# re-rendering, (c) skips the opening_balance branch (rarely wanted
# mid-invoice; the standalone /customers/new page keeps it).
@bp.route("/quick-create", methods=["POST"])
@login_required
def quick_create():
    # MARSOUD-TKT-INVOICE-INLINE-CUSTOMER — permission check is inline
    # (not via @require_permission) so a denial returns JSON 403 the
    # modal can display, not a 302 redirect to /dashboard the JS
    # client can't follow. Same permission — `partners.manage` — same
    # gate as customers.new.
    from app.services.permissions import has_permission
    if not g.active_company:
        return jsonify(ok=False, error="لا توجد شركة نشطة"), 400
    if not has_permission("partners.manage"):
        return jsonify(ok=False,
                        error="ليس لديك صلاحية لإضافة عميل"), 403

    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="الاسم مطلوب"), 400

    rep_raw = request.form.get("sales_rep_id")
    c = Customer(
        company_id=g.active_company.id,
        name=name,
        email=(request.form.get("email") or "").strip() or None,
        phone=(request.form.get("phone") or "").strip() or None,
        address=(request.form.get("address") or "").strip() or None,
        tax_number=(request.form.get("tax_number") or "").strip() or None,
        sales_rep_id=int(rep_raw) if rep_raw and rep_raw.isdigit() else None,
        commission_rate=_parse_commission_rate(request.form.get("commission_rate")),
    )
    db.session.add(c)
    db.session.flush()
    try:
        from app.services.subsidiary import ensure_customer_account
        ensure_customer_account(c)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        return jsonify(ok=False,
                        error=f"تعذّر إنشاء الحساب الفرعي للعميل: {e}"), 400
    db.session.commit()
    try:
        from app.services.activity import log_action
        log_action(action_type="CREATE", entity_type="customer",
                   entity_id=c.id, entity_label=c.name,
                   company_id=c.company_id)
    except Exception:
        pass
    return jsonify(ok=True, id=c.id, name=c.name)


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
        # MARSOUD-TKT-ADMIN-OWNER-COL — same field on edit.
        c.contact_person = (request.form.get("contact_person") or "").strip() or None
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
@require_permission("customers.view")
def view(customer_id):
    c = db.session.get(Customer, customer_id)
    if not c or c.company_id != g.active_company.id:
        return redirect(url_for("customers.index"))
    # FR-16 — list every project for this customer
    from app.models import Project
    customer_projects = Project.query.filter_by(
        customer_id=c.id,
    ).order_by(Project.created_at.desc()).all()

    # MARSOUD-REFUNDS-01 — show every refund we've issued against
    # invoices for this customer, oldest-first with a total.
    from app.models import Refund, Invoice
    customer_refunds = db.session.query(Refund, Invoice).join(
        Invoice, Refund.invoice_id == Invoice.id,
    ).filter(Invoice.customer_id == c.id).order_by(
        Refund.created_at.desc(),
    ).all()
    refunds_total = sum(float(r.amount or 0) for r, _ in customer_refunds)

    # MARSOUD-PARTY-OPENING-BALANCE-01 — read-only display.
    from app.models import PartyOpeningBalance, PartyType
    opening = PartyOpeningBalance.query.filter_by(
        company_id=c.company_id,
        party_type=PartyType.CUSTOMER, party_id=c.id,
    ).first()

    # MARSOUD-CUSTOMER-DEPOSIT-01 UI (Abdelhamid 2026-07-24) — surface
    # every deposit the customer paid us, active + history. The
    # customer view is the natural home; a global deposits page
    # doesn't fit the tenant's mental model.
    from app.models import CustomerDeposit, PaymentMethod
    deposits = CustomerDeposit.query.filter_by(
        customer_id=c.id,
    ).order_by(CustomerDeposit.date.desc(),
                CustomerDeposit.id.desc()).all()
    from app.services.deposits import total_active_amount
    active_deposits_total = total_active_amount(c.id)
    payment_methods = PaymentMethod.query.filter_by(
        company_id=c.company_id, is_active=True,
    ).order_by(PaymentMethod.is_default.desc(),
                PaymentMethod.name.asc()).all()

    return render_template(
        "customers/view.html", customer=c,
        customer_projects=customer_projects,
        customer_refunds=customer_refunds,
        refunds_total=refunds_total,
        opening_balance=opening,
        deposits=deposits,
        active_deposits_total=active_deposits_total,
        payment_methods=payment_methods,
    )


# MARSOUD-CUSTOMER-DEPOSIT-01 UI (Abdelhamid 2026-07-24) —
# post + refund actions.
#
# MARSOUD-DEPOSIT-PERMS (2026-08-05) — both of these were gated on
# `customers.view`, a READ permission held by seven roles including
# `viewer`. Taking cash from a customer and refunding cash to them are
# not read actions: each posts a balanced journal entry, so nothing
# downstream ever looks wrong. `partners.manage` (owner/admin/accountant)
# is the write-side gate the rest of this file already uses — see `new`
# and `edit` above.
@bp.route("/<int:customer_id>/deposits", methods=["POST"])
@login_required
@require_permission("partners.manage")
def receive_deposit(customer_id):
    from datetime import datetime as _dt
    from app.services.deposits import record_deposit, DepositError
    from app.models import PaymentMethod
    c = db.session.get(Customer, customer_id)
    if not c or c.company_id != g.active_company.id:
        abort(404)
    try:
        amount = float(request.form.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    pm_id = request.form.get("payment_method_id", type=int)
    pm = db.session.get(PaymentMethod, pm_id) if pm_id else None
    if pm and pm.company_id != c.company_id:
        pm = None
    date_raw = (request.form.get("date") or "").strip()
    d = None
    if date_raw:
        try:
            d = _dt.strptime(date_raw, "%Y-%m-%d").date()
        except ValueError:
            d = None
    notes = (request.form.get("notes") or "").strip() or None
    try:
        record_deposit(
            company_id=c.company_id, customer=c,
            amount=amount, payment_method=pm, date_=d,
            notes=notes, actor_id=current_user.id,
        )
        flash("تم استلام العربون", "success")
    except DepositError as e:
        flash(str(e), "error")
    return redirect(url_for("customers.view",
                              customer_id=c.id))


@bp.route("/deposits/<int:deposit_id>/refund", methods=["POST"])
@login_required
@require_permission("partners.manage")   # MARSOUD-DEPOSIT-PERMS — see above
def refund_deposit(deposit_id):
    from app.services.deposits import refund, DepositError
    from app.models import CustomerDeposit
    d = db.session.get(CustomerDeposit, deposit_id)
    if not d or d.company_id != g.active_company.id:
        abort(404)
    try:
        refund(d, actor_id=current_user.id)
        flash("تم استرداد العربون", "success")
    except DepositError as e:
        flash(str(e), "error")
    return redirect(url_for("customers.view",
                              customer_id=d.customer_id))


@bp.route("/aging")
@login_required
@require_permission("customers.view")
def aging():
    report = aging_report(g.active_company.id)
    return render_template("customers/aging.html", report=report)
