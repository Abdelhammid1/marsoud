"""MARSOUD-TKT-TREASURY-HUB-01 (2026-09-02) — الخزينة.

Unified surface: one screen, four buttons (قبض / دفع / تحويل /
شيكات-Phase-2), live balances for every cash / bank account, no
duplicate accounting logic. Every mutating call routes through the
existing services (`record_payment`, `record_bill_payment`,
`accounting_ops.transfer`) so the JE + status flip + commission
firing behave identically to today's flows.
"""
from flask import (
    Blueprint, render_template, redirect, url_for, request, g, flash,
    abort, jsonify,
)
from flask_login import login_required, current_user

from app import db
from app.models import Invoice, VendorBill
from app.models.invoice import InvoiceStatus
from app.models.vendor_bill import VendorBillStatus
from app.services.permissions import require_permission, has_permission
from app.services.ledger import LedgerError
from app.services.treasury import (
    TreasuryError, list_balances, kpi, receive, pay, transfer,
)


bp = Blueprint("treasury", __name__)


# ─── Dashboard ─────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("reports.view")
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    cid = g.active_company.id
    return render_template(
        "treasury/index.html",
        groups=list_balances(cid),
        stats=kpi(cid),
        can_operate=has_permission("treasury.operate"),
    )


# ─── Write actions ─────────────────────────────────────────────────
def _int_or_none(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


@bp.route("/receive", methods=["POST"])
@login_required
@require_permission("treasury.operate")
def receive_route():
    cid = g.active_company.id
    try:
        receive(
            cid,
            amount=request.form.get("amount"),
            account_id=_int_or_none(request.form.get("account_id")),
            source=(request.form.get("source") or "misc").strip(),
            invoice_id=_int_or_none(request.form.get("invoice_id")),
            note=request.form.get("note"),
            actor_id=current_user.id,
        )
        flash("تم تسجيل القبض", "success")
    except (TreasuryError, LedgerError) as e:
        flash(str(e), "error")
    return redirect(url_for("treasury.index"))


@bp.route("/pay", methods=["POST"])
@login_required
@require_permission("treasury.operate")
def pay_route():
    cid = g.active_company.id
    try:
        pay(
            cid,
            amount=request.form.get("amount"),
            account_id=_int_or_none(request.form.get("account_id")),
            source=(request.form.get("source") or "misc").strip(),
            vendor_bill_id=_int_or_none(request.form.get("vendor_bill_id")),
            note=request.form.get("note"),
            confirm_overdraft=(request.form.get("confirm_overdraft")
                                in ("1", "true", "on")),
            actor_id=current_user.id,
        )
        flash("تم تسجيل الدفع", "success")
    except TreasuryError as e:
        # AC #5 — overdraft is a warning, not a block. The message
        # already tells the user "أكّد السحب"; they resubmit the same
        # form with confirm_overdraft=1.
        flash(str(e), "warning" if e.code == "insufficient_funds" else "error")
    except LedgerError as e:
        flash(str(e), "error")
    return redirect(url_for("treasury.index"))


@bp.route("/transfer", methods=["POST"])
@login_required
@require_permission("treasury.operate")
def transfer_route():
    cid = g.active_company.id
    try:
        transfer(
            cid,
            from_id=_int_or_none(request.form.get("from_id")),
            to_id=_int_or_none(request.form.get("to_id")),
            amount=request.form.get("amount"),
            note=request.form.get("note"),
            actor_id=current_user.id,
        )
        flash("تم التحويل", "success")
    except (TreasuryError, LedgerError) as e:
        flash(str(e), "error")
    except Exception as e:  # noqa: BLE001 — surface builder errors too
        flash(str(e) or "فشل التحويل", "error")
    return redirect(url_for("treasury.index"))


# ─── Typeahead lookups ─────────────────────────────────────────────
@bp.route("/lookup/invoices")
@login_required
@require_permission("reports.view")
def lookup_invoices():
    """Open invoices (unpaid / partially paid / overdue / sent) for the
    receive modal's picker. Restricted to top 20 rows so a big tenant
    doesn't hang the modal."""
    cid = g.active_company.id
    q = (request.args.get("q") or "").strip()
    query = Invoice.query.filter_by(company_id=cid).filter(
        Invoice.status.in_([
            InvoiceStatus.SENT,
            InvoiceStatus.PARTIALLY_PAID,
            InvoiceStatus.OVERDUE,
        ])
    )
    if q:
        query = query.filter(Invoice.number.ilike(f"%{q}%"))
    rows = query.order_by(Invoice.issue_date.desc()).limit(20).all()
    return jsonify([
        {
            "id": inv.id, "number": inv.number,
            "customer": (inv.customer.name if inv.customer else "—"),
            "balance": float(inv.balance or 0),
            "total": float(inv.total or 0),
        } for inv in rows
    ])


@bp.route("/lookup/vendor-bills")
@login_required
@require_permission("reports.view")
def lookup_vendor_bills():
    cid = g.active_company.id
    q = (request.args.get("q") or "").strip()
    query = VendorBill.query.filter_by(company_id=cid).filter(
        VendorBill.status.in_([
            VendorBillStatus.POSTED,
            VendorBillStatus.PARTIALLY_PAID,
            VendorBillStatus.OVERDUE,
        ])
    )
    if q:
        query = query.filter(VendorBill.number.ilike(f"%{q}%"))
    rows = query.order_by(VendorBill.issue_date.desc()).limit(20).all()
    return jsonify([
        {
            "id": b.id, "number": b.number,
            "vendor": (b.vendor.name if b.vendor else "—"),
            "balance": float(b.balance or 0),
            "total": float(b.total or 0),
        } for b in rows
    ])
