from datetime import datetime, date
from flask import Blueprint, render_template, redirect, url_for, request, g, send_file
from flask_login import login_required
from app.services.reports import (
    balance_sheet, income_statement, cash_flow,
    income_summary, expenses_summary, income_statement_compared,
    aging_report, ap_aging_report, vat_report,
    payroll_summary_report, fixed_assets_report,
)
from app.services.permissions import require_permission

bp = Blueprint("reports", __name__)


def _parse_date(s, default=None):
    if not s:
        return default
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return default


@bp.route("/")
@login_required
def index():
    return render_template("reports/index.html")


@bp.route("/balance-sheet")
@login_required
def balance():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    as_of = _parse_date(request.args.get("as_of"), date.today())
    data = balance_sheet(g.active_company.id, as_of=as_of)
    return render_template("reports/balance_sheet.html", data=data, as_of=as_of)


@bp.route("/income-statement")
@login_required
def income():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    today = date.today()
    start = _parse_date(request.args.get("start_date"), today.replace(day=1))
    end = _parse_date(request.args.get("end_date"), today)
    data = income_statement(g.active_company.id, start_date=start, end_date=end)
    return render_template("reports/income_statement.html", data=data, start=start, end=end)


@bp.route("/cash-flow")
@login_required
def cashflow():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    today = date.today()
    start = _parse_date(request.args.get("start_date"), today.replace(day=1))
    end = _parse_date(request.args.get("end_date"), today)
    data = cash_flow(g.active_company.id, start_date=start, end_date=end)
    return render_template("reports/cash_flow.html", data=data, start=start, end=end)


@bp.route("/income-summary")
@login_required
def income_summary_view():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    today = date.today()
    start = _parse_date(request.args.get("start_date"), today.replace(day=1))
    end = _parse_date(request.args.get("end_date"), today)
    data = income_summary(g.active_company.id, start_date=start, end_date=end)
    return render_template("reports/income_summary.html", data=data, start=start, end=end)


@bp.route("/expenses-summary")
@login_required
def expenses_summary_view():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    today = date.today()
    start = _parse_date(request.args.get("start_date"), today.replace(day=1))
    end = _parse_date(request.args.get("end_date"), today)
    data = expenses_summary(g.active_company.id, start_date=start, end_date=end)
    return render_template("reports/expenses_summary.html", data=data, start=start, end=end)


@bp.route("/pl-compared")
@login_required
def pl_compared():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    today = date.today()
    start = _parse_date(request.args.get("start_date"), today.replace(day=1))
    end = _parse_date(request.args.get("end_date"), today)
    data = income_statement_compared(g.active_company.id, start_date=start, end_date=end)
    return render_template("reports/pl_compared.html", data=data, start=start, end=end)


@bp.route("/ar-aging")
@login_required
def ar_aging():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    as_of = _parse_date(request.args.get("as_of"), date.today())
    data = aging_report(g.active_company.id, as_of=as_of)
    return render_template("reports/ar_aging.html", data=data, as_of=as_of)


@bp.route("/ap-aging")
@login_required
def ap_aging():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    as_of = _parse_date(request.args.get("as_of"), date.today())
    data = ap_aging_report(g.active_company.id, as_of=as_of)
    return render_template("reports/ap_aging.html", data=data, as_of=as_of)


@bp.route("/vat")
@login_required
def vat():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    today = date.today()
    start = _parse_date(request.args.get("start_date"), today.replace(day=1))
    end = _parse_date(request.args.get("end_date"), today)
    data = vat_report(g.active_company.id, start_date=start, end_date=end)
    return render_template("reports/vat.html", data=data, start=start, end=end)


@bp.route("/payroll")
@login_required
def payroll():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    today = date.today()
    year = request.args.get("year", type=int)
    month = request.args.get("month", type=int)
    data = payroll_summary_report(g.active_company.id, year=year, month=month)
    return render_template("reports/payroll_summary.html", data=data, year=year, month=month, today=today)


@bp.route("/fixed-assets")
@login_required
def fixed_assets():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    data = fixed_assets_report(g.active_company.id)
    return render_template("reports/fixed_assets.html", data=data)


@bp.route("/profitability")
@login_required
def profitability():
    """ERP-02 — per-product profit (revenue − COGS) over a date range.

    Pulls InvoiceItem rows for non-voided invoices, sums revenue from
    line_total and COGS from quantity × unit_cost_at_sale (frozen at
    sale time). Groups by variant.
    """
    from app.services.permissions import has_permission
    if not has_permission("reports.profitability"):
        return redirect(url_for("reports.index"))
    from app.models import InvoiceItem, Invoice, InvoiceStatus, ProductVariant
    from app import db as _db
    today = date.today()
    start = _parse_date(request.args.get("start_date"), today.replace(day=1))
    end = _parse_date(request.args.get("end_date"), today)
    cid = g.active_company.id
    rows = (
        _db.session.query(InvoiceItem, Invoice)
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .filter(Invoice.company_id == cid,
                Invoice.issue_date >= start, Invoice.issue_date <= end,
                Invoice.status != InvoiceStatus.DRAFT,
                Invoice.status != InvoiceStatus.VOIDED,
                InvoiceItem.variant_id.isnot(None))
        .all()
    )
    agg = {}
    for item, inv in rows:
        v = item.variant
        if not v:
            continue
        a = agg.setdefault(v.id, {
            "sku": v.sku, "name": v.display_name,
            "qty": 0, "revenue": 0, "cogs": 0,
        })
        a["qty"] += float(item.quantity or 0)
        a["revenue"] += float(item.line_total or 0)
        a["cogs"] += float(item.quantity or 0) * float(item.unit_cost_at_sale or 0)
    rows_out = []
    for v_id, r in agg.items():
        gp = r["revenue"] - r["cogs"]
        gm = (gp / r["revenue"] * 100) if r["revenue"] > 0 else 0
        rows_out.append({**r, "gross_profit": gp, "gross_margin": gm})
    rows_out.sort(key=lambda r: -r["gross_profit"])
    totals = {
        "qty": sum(r["qty"] for r in rows_out),
        "revenue": sum(r["revenue"] for r in rows_out),
        "cogs": sum(r["cogs"] for r in rows_out),
        "gross_profit": sum(r["gross_profit"] for r in rows_out),
    }
    if totals["revenue"] > 0:
        totals["gross_margin"] = totals["gross_profit"] / totals["revenue"] * 100
    else:
        totals["gross_margin"] = 0
    return render_template("reports/profitability.html",
                           rows=rows_out, totals=totals,
                           start_date=start, end_date=end)


@bp.route("/cashier-sales")
@login_required
def cashier_sales():
    """ERP-02 — per-cashier sales totals + by-payment-method breakdown."""
    from app.services.permissions import has_permission
    if not has_permission("reports.cashier_sales"):
        return redirect(url_for("reports.index"))
    from app.models import Invoice, InvoiceStatus, Payment, PaymentMethod, User
    from app import db as _db
    today = date.today()
    start = _parse_date(request.args.get("start_date"), today)
    end = _parse_date(request.args.get("end_date"), today)
    cid = g.active_company.id
    pos_orders = Invoice.query.filter(
        Invoice.company_id == cid,
        Invoice.source == "POS",
        Invoice.issue_date >= start,
        Invoice.issue_date <= end,
    ).all()

    by_cashier = {}
    for inv in pos_orders:
        cid_key = inv.cashier_id or 0
        row = by_cashier.setdefault(cid_key, {
            "cashier": inv.cashier, "orders": 0, "voids": 0,
            "gross": 0, "net": 0, "by_method": {},
        })
        row["orders"] += 1
        if inv.is_voided:
            row["voids"] += 1
        else:
            row["gross"] += float(inv.total or 0)
            row["net"] += float(inv.total or 0)
        # Payment method breakdown
        for pay in inv.payments:
            mname = pay.payment_method.name_ar if pay.payment_method else (pay.method or "غير محدد")
            if inv.is_voided:
                continue
            row["by_method"][mname] = row["by_method"].get(mname, 0) + float(pay.amount or 0)
    rows_out = sorted(by_cashier.values(), key=lambda r: -r["gross"])
    return render_template("reports/cashier_sales.html",
                           rows=rows_out, start_date=start, end_date=end)


@bp.route("/<report_type>/export/<fmt>")
@login_required
def export(report_type, fmt):
    # GAP-3: profitability + cashier-sales gated by their own permissions.
    from app.services.permissions import has_permission
    if report_type == "profitability" and not has_permission("reports.profitability"):
        return redirect(url_for("reports.index"))
    if report_type == "cashier-sales" and not has_permission("reports.cashier_sales"):
        return redirect(url_for("reports.index"))

    from app.services.export import export_report
    today = date.today()
    start = _parse_date(request.args.get("start_date"), today.replace(day=1))
    end = _parse_date(request.args.get("end_date"), today)
    kwargs = {}
    if report_type == "payroll-summary":
        kwargs["year"] = request.args.get("year", type=int)
        kwargs["month"] = request.args.get("month", type=int)
    file_io, filename, mimetype = export_report(g.active_company, report_type, fmt, start, end, **kwargs)
    return send_file(file_io, as_attachment=True, download_name=filename, mimetype=mimetype)


# ─── MARSOUD-COMM-01 Phase C: sales commissions report ────────────
@bp.route("/sales-commissions")
@login_required
@require_permission("reports.view")
def sales_commissions():
    """Per-rep breakdown of sales commissions: unpaid total, paid total,
    plus a drill-down of every commission row in the date range."""
    from app.services.sales_commissions import commission_report
    today = date.today()
    start = _parse_date(request.args.get("start_date"),
                         today.replace(day=1))
    end = _parse_date(request.args.get("end_date"), today)
    rows = commission_report(g.active_company.id,
                              from_date=start, to_date=end)
    return render_template(
        "reports/sales_commissions.html",
        rep_buckets=rows, start=start, end=end,
    )
