"""Recurring vendor bills — UI routes.

Mounted at /recurring-bills. Strictly projection-only — no GL posting
happens here. The forecast page lives at /forecast (separate
blueprint registration in app/__init__.py).
"""
from datetime import date, datetime, timedelta
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g, abort,
)
from flask_login import login_required, current_user
from app import db
from app.models import (
    RecurringBill, RecurringBillOverride, VendorBill,
    INTERVAL_UNITS, OVERRIDE_ACTIONS,
)
from app.services.recurring_bills import (
    create_recurring_from_bill, deactivate_recurring,
    set_override, forecast, RecurringBillError,
)
from app.services.permissions import require_permission


bp = Blueprint("recurring_bills", __name__)


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@bp.route("/")
@login_required
@require_permission("vendor_bills.create")
def index():
    items = (RecurringBill.query
             .filter_by(company_id=g.active_company.id)
             .order_by(RecurringBill.active.desc(),
                       RecurringBill.start_date.desc())
             .all())
    return render_template("recurring_bills/index.html", items=items)


@bp.route("/from-bill/<int:bill_id>", methods=["POST"])
@login_required
@require_permission("vendor_bills.create")
def from_bill(bill_id):
    bill = db.session.get(VendorBill, bill_id)
    if not bill or bill.company_id != g.active_company.id:
        abort(404)
    try:
        rb = create_recurring_from_bill(
            bill_id=bill.id,
            interval_unit=(request.form.get("interval_unit") or "MONTH").upper(),
            interval_count=request.form.get("interval_count") or 1,
            start_date=_parse_date(request.form.get("start_date")) or date.today(),
            end_date=_parse_date(request.form.get("end_date")),
            company_id=g.active_company.id,
            user_id=current_user.id,
        )
        flash(f"تم تفعيل التكرار: {rb.label_ar} ابتداءً من "
              f"{rb.start_date.isoformat()}", "success")
        return redirect(url_for("recurring_bills.index"))
    except RecurringBillError as e:
        flash(str(e), "error")
        return redirect(url_for("vendor_bills.view", bill_id=bill_id))


@bp.route("/<int:rb_id>/deactivate", methods=["POST"])
@login_required
@require_permission("vendor_bills.create")
def deactivate(rb_id):
    try:
        deactivate_recurring(rb_id, g.active_company.id)
        flash("تم إيقاف التكرار. التوقّعات الجديدة مش هتظهر، اللي فات كما هو.",
              "success")
    except RecurringBillError as e:
        flash(str(e), "error")
    return redirect(url_for("recurring_bills.index"))


@bp.route("/<int:rb_id>/override", methods=["POST"])
@login_required
@require_permission("vendor_bills.create")
def override(rb_id):
    try:
        action = (request.form.get("action") or "").upper()
        occ = _parse_date(request.form.get("occurrence_date"))
        if not occ:
            raise RecurringBillError("تاريخ غير صالح")
        raw_amt = request.form.get("amount")
        amount = float(raw_amt) if (raw_amt and raw_amt.strip()) else None
        set_override(
            recurring_bill_id=rb_id, occurrence_date=occ,
            action=action, amount=amount,
            company_id=g.active_company.id,
        )
        flash(
            f"تم {'تعديل' if action == 'AMEND' else 'إلغاء'} توقّع {occ.isoformat()}",
            "success",
        )
    except RecurringBillError as e:
        flash(str(e), "error")
    return redirect(url_for("recurring_bills.index"))


# Forecast page lives under a separate URL prefix (/forecast)
forecast_bp = Blueprint("forecast", __name__)


@forecast_bp.route("/")
@login_required
@require_permission("vendor_bills.create")
def index():
    """Vendor-bill cash-out forecast. Filter by range=week|month|year|custom."""
    rng = (request.args.get("range") or "month").lower()
    today = date.today()
    if rng == "week":
        start, end = today, today + timedelta(days=7)
    elif rng == "year":
        start, end = today, today + timedelta(days=365)
    elif rng == "custom":
        start = _parse_date(request.args.get("from")) or today
        end = _parse_date(request.args.get("to")) or (today + timedelta(days=30))
        if end < start:
            start, end = end, start
    else:
        # default: month
        rng = "month"
        start, end = today, today + timedelta(days=30)

    data = forecast(g.active_company.id, start, end)
    return render_template(
        "recurring_bills/forecast.html",
        data=data, rng=rng, today=today,
    )
