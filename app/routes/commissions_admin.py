"""MARSOUD-COMM-DASHBOARD (Abdelhamid 2026-08-31) — standalone
commissions management dashboard.

Sits alongside — NEVER inside — the payroll pages. Every action here
uses the shared services in `app/services/sales_commissions.py` so
the accounting behaves identically whether a commission is paid from
here or as part of a payroll run. See the ticket for the full spec.

Route surface:
  * GET  /commissions/                 — dashboard with 5 tabs + KPI
  * POST /commissions/<id>/settle      — individual manual settlement
  * POST /commissions/bulk-settle      — settle a checked-list
  * POST /commissions/<id>/void        — cancel unpaid (mandatory reason)
  * GET  /commissions/rep/<user_id>    — rep detail (history + actions)

Everything mutating is gated on `payroll.accruals` — the same
permission that owns the settle-accrual flow inside payroll (posting
a journal is the financial-only side). Read-only tabs (السجل +
حسب المندوب) fall back to `reports.view` so an accountant / viewer
can inspect without being able to settle.
"""
from datetime import datetime, date
from decimal import Decimal
from collections import defaultdict

from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g, abort,
)
from flask_login import login_required, current_user
from sqlalchemy import and_, or_

from app import db
from app.models import (
    SalesCommission, User, Employee, Customer, Invoice, PaymentMethod,
)
from app.models.journal import JournalEntry
from app.services.sales_commissions import (
    settle_commission_manual, void_commission,
)
from app.services.ledger import LedgerError
from app.services.permissions import require_permission


bp = Blueprint("commissions_admin", __name__)


def _base_query():
    """Cross-tenant safe query — all commissions for the active
    company. All list variants derive from this."""
    return SalesCommission.query.filter_by(
        company_id=g.active_company.id)


def _apply_common_filters(q):
    """URL-param filters shared by every list tab."""
    rep_id = request.args.get("rep_id", type=int)
    if rep_id:
        q = q.filter(SalesCommission.sales_rep_id == rep_id)
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    if year:
        q = q.filter(SalesCommission.period_year == year)
    if month:
        q = q.filter(SalesCommission.period_month == month)
    start_raw = (request.args.get("start_date") or "").strip()
    end_raw = (request.args.get("end_date") or "").strip()

    def _p(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date() if s else None
        except ValueError:
            return None
    sd, ed = _p(start_raw), _p(end_raw)
    if sd:
        q = q.filter(SalesCommission.created_at >= datetime.combine(
            sd, datetime.min.time()))
    if ed:
        q = q.filter(SalesCommission.created_at <= datetime.combine(
            ed, datetime.max.time()))
    return q


def _sales_reps():
    """Every user linked to the active company who has ever received
    a commission — for the filter dropdown."""
    ids = {c.sales_rep_id for c in _base_query().all() if c.sales_rep_id}
    if not ids:
        return []
    return (User.query.filter(User.id.in_(ids))
            .order_by(User.full_name).all())


def _kpi_cards():
    """Dashboard KPI aggregates. Runs a couple of grouped counts +
    sums — small enough for a per-request cost."""
    from sqlalchemy import func
    cid = g.active_company.id

    # Unpaid + not voided
    unpaid_total = float(db.session.query(
        func.sum(SalesCommission.amount)
    ).filter(
        SalesCommission.company_id == cid,
        SalesCommission.status == "UNPAID",
        SalesCommission.voided_at.is_(None),
    ).scalar() or 0)

    # Paid this calendar month
    today = date.today()
    settled_this_month = float(db.session.query(
        func.sum(SalesCommission.settled_amount)
    ).filter(
        SalesCommission.company_id == cid,
        SalesCommission.status == "PAID",
        SalesCommission.settled_at >= datetime(
            today.year, today.month, 1),
    ).scalar() or 0)

    pending_count = (SalesCommission.query
                     .filter_by(company_id=cid, status="UNPAID")
                     .filter(SalesCommission.voided_at.is_(None))
                     .count())

    # Top-3 reps by unpaid
    top_owed_rows = (
        db.session.query(
            User.id, User.full_name,
            func.sum(SalesCommission.amount).label("unpaid"))
        .join(SalesCommission, SalesCommission.sales_rep_id == User.id)
        .filter(SalesCommission.company_id == cid,
                SalesCommission.status == "UNPAID",
                SalesCommission.voided_at.is_(None))
        .group_by(User.id, User.full_name)
        .order_by(func.sum(SalesCommission.amount).desc())
        .limit(3).all()
    )
    top_owed = [
        {"id": r[0], "name": r[1], "amount": float(r[2] or 0)}
        for r in top_owed_rows
    ]
    return {
        "unpaid_total": round(unpaid_total, 2),
        "settled_this_month": round(settled_this_month, 2),
        "pending_count": pending_count,
        "top_owed": top_owed,
    }


def _by_rep_rows():
    """One row per rep with totals — powers the 'حسب المندوب' tab."""
    from sqlalchemy import func
    cid = g.active_company.id
    rows = (
        db.session.query(
            User.id, User.full_name, User.email,
            func.sum(SalesCommission.amount).filter(
                and_(SalesCommission.status == "UNPAID",
                     SalesCommission.voided_at.is_(None))).label("unpaid"),
            func.sum(SalesCommission.settled_amount).filter(
                SalesCommission.status == "PAID").label("paid"),
            func.max(SalesCommission.settled_at).label("last_settle"),
        )
        .join(SalesCommission, SalesCommission.sales_rep_id == User.id)
        .filter(SalesCommission.company_id == cid)
        .group_by(User.id, User.full_name, User.email)
        .order_by(func.coalesce(func.sum(SalesCommission.amount).filter(
            SalesCommission.status == "UNPAID"), 0).desc())
        .all()
    )
    return [
        {"id": r[0], "name": r[1], "email": r[2],
         "unpaid": float(r[3] or 0), "paid": float(r[4] or 0),
         "last_settle": r[5]}
        for r in rows
    ]


def _payment_methods():
    return (PaymentMethod.query
            .filter_by(company_id=g.active_company.id, is_active=True)
            .order_by(PaymentMethod.is_default.desc(),
                      PaymentMethod.name.asc()).all())


@bp.route("/")
@login_required
@require_permission("reports.view")
def index():
    tab = (request.args.get("tab") or "unpaid").strip()
    if tab not in ("all", "unpaid", "paid", "by_rep", "voided"):
        tab = "unpaid"

    q = _apply_common_filters(_base_query())

    if tab == "unpaid":
        rows = (q.filter(SalesCommission.status == "UNPAID")
                .filter(SalesCommission.voided_at.is_(None))
                .order_by(SalesCommission.created_at.desc()).all())
    elif tab == "paid":
        rows = (q.filter(SalesCommission.status == "PAID")
                .order_by(SalesCommission.settled_at.desc()).all())
    elif tab == "voided":
        rows = (q.filter(SalesCommission.voided_at.isnot(None))
                .order_by(SalesCommission.voided_at.desc()).all())
    else:  # all
        rows = q.order_by(SalesCommission.created_at.desc()).all()

    by_rep = _by_rep_rows() if tab == "by_rep" else []

    return render_template(
        "commissions_admin/dashboard.html",
        tab=tab, rows=rows, by_rep=by_rep,
        reps=_sales_reps(),
        payment_methods=_payment_methods(),
        kpi=_kpi_cards(),
        filters={
            "rep_id": request.args.get("rep_id", type=int),
            "year": request.args.get("year", type=int),
            "month": request.args.get("month", type=int),
            "start_date": request.args.get("start_date") or "",
            "end_date": request.args.get("end_date") or "",
        },
    )


@bp.route("/<int:comm_id>/settle", methods=["POST"])
@login_required
@require_permission("payroll.accruals")
def settle(comm_id):
    comm = db.session.get(SalesCommission, comm_id)
    if not comm or comm.company_id != g.active_company.id:
        abort(404)
    if comm.voided_at is not None:
        flash("لا يمكن سداد عمولة ملغاة", "error")
        return redirect(url_for("commissions_admin.index"))

    amount_raw = (request.form.get("amount") or "").strip()
    pay_amount = None
    if amount_raw:
        try:
            pay_amount = float(amount_raw)
        except ValueError:
            flash("قيمة السداد غير صالحة", "error")
            return redirect(url_for("commissions_admin.index",
                                     tab="unpaid"))

    payment_code = (request.form.get("payment_account_code") or "1110").strip()
    try:
        settle_commission_manual(
            comm, amount=pay_amount,
            payment_account_code=payment_code,
            created_by=current_user.id,
        )
        flash("تم سداد العمولة وترحيل القيد", "success")
    except LedgerError as e:
        flash(str(e), "error")
    return redirect(url_for("commissions_admin.index", tab="unpaid"))


@bp.route("/bulk-settle", methods=["POST"])
@login_required
@require_permission("payroll.accruals")
def bulk_settle():
    ids = request.form.getlist("commission_ids")
    payment_code = (request.form.get("payment_account_code")
                     or "1110").strip()
    if not ids:
        flash("لم تختر أي عمولة", "warning")
        return redirect(url_for("commissions_admin.index",
                                 tab="unpaid"))
    settled, failed = 0, []
    for raw in ids:
        try:
            cid = int(raw)
        except ValueError:
            continue
        comm = db.session.get(SalesCommission, cid)
        if not comm or comm.company_id != g.active_company.id:
            continue
        if comm.voided_at is not None:
            failed.append(f"#{cid} ملغاة")
            continue
        try:
            settle_commission_manual(
                comm, amount=None,
                payment_account_code=payment_code,
                created_by=current_user.id,
            )
            settled += 1
        except LedgerError as e:
            failed.append(f"#{cid}: {e}")
    if settled:
        flash(f"تم سداد {settled} عمولة", "success")
    if failed:
        flash("لم تُسدد بعض العمولات: " + "؛ ".join(failed), "warning")
    return redirect(url_for("commissions_admin.index", tab="unpaid"))


@bp.route("/<int:comm_id>/void", methods=["POST"])
@login_required
@require_permission("payroll.accruals")
def void(comm_id):
    comm = db.session.get(SalesCommission, comm_id)
    if not comm or comm.company_id != g.active_company.id:
        abort(404)
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("سبب الإلغاء مطلوب", "error")
        return redirect(url_for("commissions_admin.index", tab="unpaid"))
    try:
        void_commission(comm, reason=reason, actor_id=current_user.id)
        flash("تم إلغاء العمولة وترحيل قيد العكس", "success")
    except LedgerError as e:
        flash(str(e), "error")
    return redirect(url_for("commissions_admin.index", tab="voided"))


@bp.route("/rep/<int:user_id>")
@login_required
@require_permission("reports.view")
def rep_detail(user_id):
    u = db.session.get(User, user_id) or abort(404)
    rows = (_base_query()
            .filter(SalesCommission.sales_rep_id == user_id)
            .order_by(SalesCommission.created_at.desc()).all())
    total_unpaid = round(sum(float(r.amount or 0) for r in rows
                              if r.status == "UNPAID"
                              and r.voided_at is None), 2)
    total_paid = round(sum(float(r.settled_amount or 0) for r in rows
                            if r.status == "PAID"), 2)
    return render_template(
        "commissions_admin/rep_detail.html",
        rep=u, rows=rows,
        total_unpaid=total_unpaid,
        total_paid=total_paid,
        payment_methods=_payment_methods(),
    )
