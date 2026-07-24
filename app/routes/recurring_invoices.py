"""MARSOUD-RECURRING-INVOICE-01 (Abdelhamid 2026-07-24) — UI.

Customer-facing screens for the recurring-invoice service:
  · list + activate/deactivate/delete existing schedules.
  · create-from-invoice endpoint (posted from the "اجعلها متكررة"
    button on the invoice detail page).
"""
import json
from datetime import date, datetime
from decimal import Decimal
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request,
    g, abort,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    RecurringInvoice, Invoice, InvoiceItem,
    REC_INV_FREQ_MONTHLY, ALL_REC_INV_FREQS, REC_INV_FREQ_LABELS_AR,
)
from app.services.permissions import require_permission


bp = Blueprint("recurring_invoices", __name__)


@bp.route("/")
@login_required
@require_permission("invoices.create")
def index():
    """List every schedule for the active company (soft-deleted excluded)."""
    rows = RecurringInvoice.query.filter_by(
        company_id=g.active_company.id,
        is_deleted=False,
    ).order_by(RecurringInvoice.next_run_date.asc()).all()
    return render_template(
        "recurring_invoices/index.html",
        rows=rows,
        freq_labels=REC_INV_FREQ_LABELS_AR,
        today=date.today(),
    )


@bp.route("/from-invoice/<int:invoice_id>", methods=["POST"])
@login_required
@require_permission("invoices.create")
def create_from_invoice(invoice_id):
    """One-click "اجعلها متكررة" — clones the source invoice's lines
    into a new RecurringInvoice at the requested frequency."""
    inv = db.session.get(Invoice, invoice_id)
    if not inv or inv.company_id != g.active_company.id:
        abort(404)
    if not inv.customer_id:
        flash("لا يمكن جعل فاتورة زبون نقدي متكررة — اختر عميل مسجّل.",
              "error")
        return redirect(url_for("invoices.view", invoice_id=inv.id))

    frequency = (request.form.get("frequency") or
                  REC_INV_FREQ_MONTHLY).strip().upper()
    if frequency not in ALL_REC_INV_FREQS:
        frequency = REC_INV_FREQ_MONTHLY
    end_raw = (request.form.get("end_date") or "").strip()
    end_date = None
    if end_raw:
        try:
            end_date = datetime.strptime(end_raw, "%Y-%m-%d").date()
        except ValueError:
            flash("تاريخ الانتهاء غير صحيح.", "error")
            return redirect(url_for("invoices.view",
                                     invoice_id=inv.id))

    next_run_raw = (request.form.get("next_run_date") or "").strip()
    if next_run_raw:
        try:
            next_run = datetime.strptime(next_run_raw,
                                           "%Y-%m-%d").date()
        except ValueError:
            next_run = date.today()
    else:
        next_run = date.today()

    items = [
        {
            "description": it.description,
            "quantity": float(it.quantity or 0),
            "unit_price": float(it.unit_price or 0),
        }
        for it in inv.items
    ]
    if not items:
        flash("الفاتورة لا تحتوي على بنود.", "error")
        return redirect(url_for("invoices.view", invoice_id=inv.id))

    name = (request.form.get("name") or "").strip() \
        or f"متكررة من فاتورة {inv.number}"

    sched = RecurringInvoice(
        company_id=inv.company_id,
        customer_id=inv.customer_id,
        name=name[:200],
        frequency=frequency,
        next_run_date=next_run,
        end_date=end_date,
        tax_rate=inv.tax_rate,
        is_active=True,
        created_by_id=current_user.id,
    )
    sched.set_items(items)
    db.session.add(sched); db.session.commit()
    flash("تم إنشاء الجدولة. سيتم إصدار الفواتير تلقائياً في مواعيدها.",
          "success")
    return redirect(url_for("recurring_invoices.index"))


@bp.route("/<int:sched_id>/toggle", methods=["POST"])
@login_required
@require_permission("invoices.create")
def toggle(sched_id):
    sched = _owned_or_404(sched_id)
    sched.is_active = not sched.is_active
    db.session.commit()
    flash("تم تفعيل الجدولة" if sched.is_active else "تم إيقافها",
          "success")
    return redirect(url_for("recurring_invoices.index"))


@bp.route("/<int:sched_id>/delete", methods=["POST"])
@login_required
@require_permission("invoices.create")
def delete(sched_id):
    """Soft-delete: is_deleted=True so the log history stays queryable
    but the schedule stops firing + disappears from the list."""
    sched = _owned_or_404(sched_id)
    sched.is_active = False
    sched.is_deleted = True
    db.session.commit()
    flash("تم حذف الجدولة", "success")
    return redirect(url_for("recurring_invoices.index"))


def _owned_or_404(sched_id):
    sched = db.session.get(RecurringInvoice, sched_id)
    if not sched or sched.company_id != g.active_company.id:
        abort(404)
    return sched
