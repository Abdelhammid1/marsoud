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
    from app.models import (
        InvoiceItem, Invoice, InvoiceStatus, ProductVariant,
        Product, ProductGroup, ProductCategory,
    )
    from app import db as _db
    today = date.today()
    start = _parse_date(request.args.get("start_date"), today.replace(day=1))
    end = _parse_date(request.args.get("end_date"), today)
    cid = g.active_company.id
    # MARSOUD-PRODUCT-HIERARCHY-01 — optional group + category filter.
    filter_group_id = request.args.get("group_id", type=int)
    filter_category_id = request.args.get("category_id", type=int)
    group_by = request.args.get("group_by", "product")   # "product" | "group" | "category"

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

    def _passes_filter(product):
        if filter_category_id and (not product or product.category_id != filter_category_id):
            return False
        if filter_group_id and product:
            cat = product.category
            if not cat or cat.group_id != filter_group_id:
                return False
        return True

    agg = {}
    for item, inv in rows:
        v = item.variant
        if not v:
            continue
        product = v.product
        if not _passes_filter(product):
            continue
        # Bucket key depends on group_by mode.
        if group_by == "group":
            grp = product.category.group if product and product.category else None
            key = ("group", grp.id if grp else 0)
            label_sku = grp.name if grp else "— بدون —"
            label_name = grp.name if grp else "— بدون تصنيف —"
        elif group_by == "category":
            cat = product.category if product else None
            key = ("category", cat.id if cat else 0)
            label_sku = cat.name if cat else "— بدون —"
            label_name = (f"{cat.group.name} / {cat.name}"
                            if cat else "— بدون تصنيف —")
        else:
            key = ("variant", v.id)
            label_sku = v.sku
            label_name = v.display_name
        a = agg.setdefault(key, {
            "sku": label_sku, "name": label_name,
            "qty": 0, "revenue": 0, "cogs": 0,
        })
        a["qty"] += float(item.quantity or 0)
        a["revenue"] += float(item.line_total or 0)
        a["cogs"] += float(item.quantity or 0) * float(item.unit_cost_at_sale or 0)
    rows_out = []
    for _key, r in agg.items():
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
    groups = ProductGroup.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(ProductGroup.name).all()
    categories = ProductCategory.query.filter_by(
        company_id=cid, is_active=True,
    ).order_by(ProductCategory.name).all()
    return render_template("reports/profitability.html",
                           rows=rows_out, totals=totals,
                           start_date=start, end_date=end,
                           groups=groups, categories=categories,
                           filter_group_id=filter_group_id,
                           filter_category_id=filter_category_id,
                           group_by=group_by)


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


# ─── MARSOUD-EMPLOYEE-DAILY-REPORTS — owner-side viewing ────────────
@bp.route("/employees")
@login_required
@require_permission("employee_reports.view")
def employees_index():
    """One card per employee this viewer is allowed to see."""
    from flask_login import current_user
    from app.services.daily_digest import visible_employees_for
    from app.models import EmployeeDailyReport, DailyReportStatus
    employees = visible_employees_for(current_user, g.active_company.id)
    cards = []
    for e in employees:
        count = EmployeeDailyReport.query.filter_by(
            employee_id=e.id, status=DailyReportStatus.SUBMITTED,
        ).count()
        cards.append({"employee": e, "count": count})
    return render_template(
        "reports/employees_index.html", cards=cards,
    )


@bp.route("/employees/<int:employee_id>")
@login_required
@require_permission("employee_reports.view")
def employee_reports_list(employee_id):
    from flask_login import current_user
    from app.services.daily_digest import can_view_reports_for
    from app.models import Employee, EmployeeDailyReport, DailyReportStatus
    from flask import abort
    if not can_view_reports_for(current_user, employee_id,
                                  g.active_company.id):
        abort(404)
    emp = Employee.query.get_or_404(employee_id)
    reports = EmployeeDailyReport.query.filter_by(
        employee_id=emp.id, status=DailyReportStatus.SUBMITTED,
    ).order_by(EmployeeDailyReport.report_date.desc()).all()
    return render_template(
        "reports/employee_reports_list.html", emp=emp, reports=reports,
    )


@bp.route("/employees/<int:employee_id>/<int:report_id>")
@login_required
@require_permission("employee_reports.view")
def employee_report_detail(employee_id, report_id):
    from flask_login import current_user
    from app.services.daily_digest import can_view_reports_for
    from app.models import EmployeeDailyReport, DailyReportStatus
    from flask import abort
    if not can_view_reports_for(current_user, employee_id,
                                  g.active_company.id):
        abort(404)
    r = EmployeeDailyReport.query.get_or_404(report_id)
    if r.employee_id != employee_id:
        abort(404)
    if r.status != DailyReportStatus.SUBMITTED:
        abort(404)
    return render_template(
        "reports/employee_report_detail.html", report=r,
    )


# ─── MARSOUD-VENDOR-SUBCAT (Abdelhamid 2026-07-14) ──────────────────────
# Report: totals per vendor + sub-category. Optional vendor filter +
# date range. Zero-spend combinations are hidden. Uncategorized lines
# roll up under a "بدون تصنيف" bucket per vendor so legacy spend
# isn't invisible.
@bp.route("/vendor-sub-categories")
@login_required
@require_permission("vendor_bills.create")
def vendor_sub_categories():
    from app.services.vendor_sub_categories import report_totals_by_vendor
    from app.models import Vendor
    cid = g.active_company.id
    vendor_filter = request.args.get("vendor") or ""
    date_from = _parse_date(request.args.get("from"))
    date_to = _parse_date(request.args.get("to"))
    vendor_id = None
    if vendor_filter:
        try:
            vendor_id = int(vendor_filter)
        except (TypeError, ValueError):
            vendor_id = None
    rows = report_totals_by_vendor(
        cid, vendor_id=vendor_id,
        date_from=date_from, date_to=date_to,
    )
    # Roll rows into a {vendor_name → {sub_name → subtotal, ...}}
    # dict so the template can render one section per vendor with
    # its own subtotal line.
    grouped = {}
    for r in rows:
        bucket = grouped.setdefault(
            r["vendor_name"],
            {"vendor_id": r["vendor_id"], "rows": [], "total": 0.0})
        bucket["rows"].append(r)
        bucket["total"] += r["total"]
    grand_total = sum(b["total"] for b in grouped.values())
    vendors = Vendor.query.filter_by(company_id=cid).order_by(Vendor.name).all()
    return render_template(
        "reports/vendor_sub_categories.html",
        grouped=grouped, grand_total=grand_total,
        vendors=vendors, vendor_filter=vendor_filter,
        date_from=request.args.get("from") or "",
        date_to=request.args.get("to") or "",
    )

