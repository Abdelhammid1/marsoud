"""MARSOUD-REFUNDS-01 — unified refunds page (sales + purchases in one).

A single `/refunds` view that shows both `Refund` (sales) and
`VendorBillRefund` (purchases) side by side, with filters. The report
view mirrors the same union but aggregates by period / party / type.

The individual "issue a refund" actions still happen on the invoice /
vendor-bill detail pages — this blueprint is read-only + reporting.
"""
from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, request, g, redirect, url_for
from flask_login import login_required
from app import db
from app.models import (
    Invoice, VendorBill, Customer, Vendor,
    Refund, RefundType, CreditNote,
    VendorBillRefund, VendorRefundType, DebitNote,
)
from app.services.permissions import require_permission


bp = Blueprint("refunds", __name__)


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _collect_refunds(company_id, start_date=None, end_date=None,
                       party_q=None, side=None, rtype=None):
    """Union sales + purchase refunds into a single list of dicts,
    then filter/sort in Python. Volume is small — refunds are rare
    events, so we don't need to push everything to SQL.
    """
    rows = []

    if side in (None, "SALES"):
        sq = db.session.query(Refund, Invoice, Customer).join(
            Invoice, Refund.invoice_id == Invoice.id,
        ).outerjoin(Customer, Invoice.customer_id == Customer.id).filter(
            Invoice.company_id == company_id,
        )
        if start_date:
            sq = sq.filter(Refund.created_at >= start_date)
        if end_date:
            sq = sq.filter(Refund.created_at < end_date + timedelta(days=1))
        if party_q:
            like = f"%{party_q}%"
            sq = sq.filter(Customer.name.ilike(like))
        if rtype and rtype in {t.value for t in RefundType}:
            sq = sq.filter(Refund.type == RefundType[rtype])
        for r, inv, cust in sq.all():
            rows.append({
                "id": r.id,
                "side": "SALES",
                "side_ar": "مبيعات",
                "number": r.number or f"REF-{inv.number}",
                "type": r.type.value,
                "type_ar": {
                    "FULL": "مرتجع كامل",
                    "PARTIAL": "مرتجع جزئي",
                    "CREDIT_NOTE": "إشعار دائن",
                }[r.type.value],
                "amount": float(r.amount or 0),
                "party_id": cust.id if cust else None,
                "party_name": cust.name if cust else "—",
                "party_kind": "customer",
                "source_ref": inv.number,
                "source_url": url_for("invoices.view",
                                        invoice_id=inv.id),
                "created_at": r.created_at,
                "reason": r.reason or "",
            })

    if side in (None, "PURCHASES"):
        pq = db.session.query(VendorBillRefund, VendorBill, Vendor).join(
            VendorBill, VendorBillRefund.bill_id == VendorBill.id,
        ).outerjoin(Vendor, VendorBill.vendor_id == Vendor.id).filter(
            VendorBillRefund.company_id == company_id,
        )
        if start_date:
            pq = pq.filter(VendorBillRefund.created_at >= start_date)
        if end_date:
            pq = pq.filter(
                VendorBillRefund.created_at < end_date + timedelta(days=1)
            )
        if party_q:
            like = f"%{party_q}%"
            pq = pq.filter(Vendor.name.ilike(like))
        if rtype and rtype in {t.value for t in VendorRefundType}:
            pq = pq.filter(VendorBillRefund.type == VendorRefundType[rtype])
        for r, bill, vend in pq.all():
            rows.append({
                "id": r.id,
                "side": "PURCHASES",
                "side_ar": "مشتريات",
                "number": r.number or "",
                "type": r.type.value,
                "type_ar": r.type.label_ar,
                "amount": float(r.amount or 0),
                "party_id": vend.id if vend else None,
                "party_name": vend.name if vend else "—",
                "party_kind": "vendor",
                "source_ref": bill.number,
                "source_url": url_for("vendor_bills.view",
                                        bill_id=bill.id),
                "created_at": r.created_at,
                "reason": r.reason or "",
            })

    rows.sort(key=lambda r: r["created_at"] or datetime.min, reverse=True)
    return rows


@bp.route("/")
@login_required
@require_permission("refunds.view")
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))

    start_date = _parse_date(request.args.get("start_date"))
    end_date = _parse_date(request.args.get("end_date"))
    party_q = (request.args.get("party") or "").strip() or None
    side = request.args.get("side") or None
    rtype = request.args.get("type") or None

    rows = _collect_refunds(
        g.active_company.id,
        start_date=start_date, end_date=end_date,
        party_q=party_q, side=side, rtype=rtype,
    )

    totals = {
        "sales_count": sum(1 for r in rows if r["side"] == "SALES"),
        "sales_amount": sum(r["amount"] for r in rows if r["side"] == "SALES"),
        "purchases_count": sum(1 for r in rows if r["side"] == "PURCHASES"),
        "purchases_amount": sum(
            r["amount"] for r in rows if r["side"] == "PURCHASES"
        ),
    }

    return render_template(
        "refunds/index.html",
        rows=rows, totals=totals,
        start_date=request.args.get("start_date") or "",
        end_date=request.args.get("end_date") or "",
        party=party_q or "",
        side=side or "",
        rtype=rtype or "",
    )


@bp.route("/report")
@login_required
@require_permission("refunds.view")
def report():
    """Group-by report: by month / by party / by type."""
    if not g.active_company:
        return redirect(url_for("companies.new"))

    start_date = _parse_date(request.args.get("start_date"))
    end_date = _parse_date(request.args.get("end_date"))
    # Default: last 12 months if no range provided.
    if not start_date:
        start_date = date.today().replace(day=1) - timedelta(days=365)
    if not end_date:
        end_date = date.today()

    rows = _collect_refunds(
        g.active_company.id,
        start_date=start_date, end_date=end_date,
    )

    by_month = {}
    by_party = {}
    by_type = {"sales": {}, "purchases": {}}
    for r in rows:
        # by month key = YYYY-MM
        mkey = r["created_at"].strftime("%Y-%m") if r["created_at"] else "?"
        b = by_month.setdefault(
            mkey, {"sales": 0.0, "purchases": 0.0, "count": 0},
        )
        b["count"] += 1
        if r["side"] == "SALES":
            b["sales"] += r["amount"]
        else:
            b["purchases"] += r["amount"]

        pkey = (r["party_kind"], r["party_id"], r["party_name"])
        b = by_party.setdefault(
            pkey, {"amount": 0.0, "count": 0, "side": r["side_ar"]},
        )
        b["amount"] += r["amount"]
        b["count"] += 1

        bucket = by_type["sales" if r["side"] == "SALES" else "purchases"]
        b = bucket.setdefault(r["type_ar"], {"amount": 0.0, "count": 0})
        b["amount"] += r["amount"]
        b["count"] += 1

    return render_template(
        "refunds/report.html",
        start_date=start_date, end_date=end_date,
        by_month=sorted(by_month.items(), reverse=True),
        by_party=sorted(
            by_party.items(), key=lambda kv: kv[1]["amount"], reverse=True,
        ),
        by_type=by_type,
        grand_total=sum(r["amount"] for r in rows),
        grand_count=len(rows),
    )
