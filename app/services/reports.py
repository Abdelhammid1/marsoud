"""Financial report generators: Balance Sheet, Income Statement, Cash Flow."""
from datetime import date
from sqlalchemy import func, and_
from app import db
from app.models import Account, AccountType, JournalLine, JournalEntry


def _account_balance(account_id, start_date=None, end_date=None):
    """Sum debits/credits for an account, excluding paused journals."""
    q = db.session.query(
        func.coalesce(func.sum(JournalLine.debit_base), 0),
        func.coalesce(func.sum(JournalLine.credit_base), 0),
    ).select_from(JournalLine).join(JournalEntry).filter(
        JournalLine.account_id == account_id,
        JournalEntry.is_active.is_(True),
    )
    if start_date:
        q = q.filter(JournalEntry.date >= start_date)
    if end_date:
        q = q.filter(JournalEntry.date <= end_date)
    debit, credit = q.first()
    return float(debit or 0), float(credit or 0)


def _signed_balance(account, debit, credit):
    if account.normal_side.value == "DEBIT":
        return debit - credit
    return credit - debit


def balance_sheet(company_id, as_of=None):
    """Snapshot of Assets, Liabilities, Equity as of a date."""
    as_of = as_of or date.today()
    accounts = Account.query.filter_by(company_id=company_id, is_active=True).all()

    result = {"assets": [], "liabilities": [], "equity": [], "as_of": as_of}
    totals = {"assets": 0.0, "liabilities": 0.0, "equity": 0.0}

    for acc in accounts:
        debit, credit = _account_balance(acc.id, end_date=as_of)
        bal = _signed_balance(acc, debit, credit)
        if abs(bal) < 0.01 and not acc.children:
            continue
        item = {"code": acc.code, "name": acc.name_ar or acc.name, "balance": bal}
        if acc.type == AccountType.ASSET:
            result["assets"].append(item)
            totals["assets"] += bal
        elif acc.type == AccountType.LIABILITY:
            result["liabilities"].append(item)
            totals["liabilities"] += bal
        elif acc.type == AccountType.EQUITY:
            result["equity"].append(item)
            totals["equity"] += bal

    net_income = _net_income(company_id, end_date=as_of)
    if abs(net_income) > 0.01:
        result["equity"].append({
            "code": "RE", "name": "صافي الربح للفترة", "balance": net_income
        })
        totals["equity"] += net_income

    result["totals"] = totals
    result["total_liab_equity"] = totals["liabilities"] + totals["equity"]
    result["balanced"] = abs(totals["assets"] - result["total_liab_equity"]) < 0.01
    return result


def _net_income(company_id, start_date=None, end_date=None):
    revenue = 0.0
    expense = 0.0
    accounts = Account.query.filter_by(company_id=company_id).all()
    for acc in accounts:
        d, c = _account_balance(acc.id, start_date=start_date, end_date=end_date)
        bal = _signed_balance(acc, d, c)
        if acc.type == AccountType.REVENUE:
            revenue += bal
        elif acc.type == AccountType.EXPENSE:
            expense += bal
    return revenue - expense


def income_statement(company_id, start_date=None, end_date=None):
    end_date = end_date or date.today()
    accounts = Account.query.filter_by(company_id=company_id, is_active=True).all()

    result = {
        "revenue": [], "expenses": [],
        "start_date": start_date, "end_date": end_date,
    }
    total_revenue = 0.0
    total_expense = 0.0

    for acc in accounts:
        d, c = _account_balance(acc.id, start_date=start_date, end_date=end_date)
        bal = _signed_balance(acc, d, c)
        if abs(bal) < 0.01:
            continue
        item = {"code": acc.code, "name": acc.name_ar or acc.name, "balance": bal}
        if acc.type == AccountType.REVENUE:
            result["revenue"].append(item)
            total_revenue += bal
        elif acc.type == AccountType.EXPENSE:
            result["expenses"].append(item)
            total_expense += bal

    result["total_revenue"] = total_revenue
    result["total_expense"] = total_expense
    result["net_income"] = total_revenue - total_expense
    return result


def _classify_cashflow_entry(entry, cash_ids):
    """Pick a cash flow category for a journal entry that touches cash.

    Resolution order:
      1. Manual override on the entry (entry.cashflow_category).
      2. source_type hint (asset_purchase / invoice / payment / payroll / vendor_bill).
      3. Account-code inference from the *non-cash* lines:
           - 12xx (excl. 1290 accumulated dep) and inventory 1140  → INVESTING
           - 3xxx equity or 21xx/22xx long-term liabilities         → FINANCING
           - depreciation pair (5250 ↔ 1290)                         → NONCASH
           - everything else (4xxx revenue / 5xxx expense / AR/AP) → OPERATING
    """
    if entry.cashflow_category in ("OPERATING", "INVESTING", "FINANCING", "NONCASH"):
        return entry.cashflow_category

    if entry.source_type == "asset_purchase":
        return "INVESTING"
    if entry.source_type == "depreciation":
        return "NONCASH"

    has_dep_expense = False
    has_acc_dep = False
    cats = set()
    for line in entry.lines:
        if line.account_id in cash_ids:
            continue
        code = line.account.code or ""
        if code == "5250":
            has_dep_expense = True
        if code == "1290":
            has_acc_dep = True
        if code in ("1300",) or (code.startswith("12") and code != "1290"):
            cats.add("INVESTING")
        elif code.startswith("3"):
            cats.add("FINANCING")
        elif code.startswith("21") or code.startswith("22"):
            cats.add("FINANCING")
        else:
            cats.add("OPERATING")

    if has_dep_expense and has_acc_dep:
        return "NONCASH"

    # Priority if mixed: investing > financing > operating
    for cat in ("INVESTING", "FINANCING", "OPERATING"):
        if cat in cats:
            return cat
    return "OPERATING"


def cash_flow(company_id, start_date=None, end_date=None):
    """Cash Flow Statement using cash account movements categorized by account-code inference.

    Categories: OPERATING / INVESTING / FINANCING. NONCASH entries (e.g., depreciation
    that doesn't touch 1110/1120) are naturally excluded because they don't hit a cash
    account at all. If an entry that touches cash is marked NONCASH (rare — only when
    the user overrides), it's still excluded.
    """
    end_date = end_date or date.today()
    cash_codes = ["1110", "1120"]
    cash_accounts = Account.query.filter(
        Account.company_id == company_id, Account.code.in_(cash_codes)
    ).all()
    cash_ids = [a.id for a in cash_accounts]

    operating = 0.0
    investing = 0.0
    financing = 0.0

    if cash_ids:
        entries = (
            JournalEntry.query.join(JournalLine)
            .filter(
                JournalEntry.company_id == company_id,
                JournalEntry.is_active.is_(True),
                JournalLine.account_id.in_(cash_ids),
            )
        )
        if start_date:
            entries = entries.filter(JournalEntry.date >= start_date)
        if end_date:
            entries = entries.filter(JournalEntry.date <= end_date)

        for entry in entries.distinct():
            cash_flow_amt = 0.0
            for line in entry.lines:
                if line.account_id in cash_ids:
                    cash_flow_amt += float(line.debit_base) - float(line.credit_base)

            category = _classify_cashflow_entry(entry, cash_ids)
            if category == "INVESTING":
                investing += cash_flow_amt
            elif category == "FINANCING":
                financing += cash_flow_amt
            elif category == "OPERATING":
                operating += cash_flow_amt
            # NONCASH → excluded

    return {
        "operating": operating,
        "investing": investing,
        "financing": financing,
        "net_change": operating + investing + financing,
        "start_date": start_date,
        "end_date": end_date,
    }


def income_summary(company_id, start_date=None, end_date=None):
    """Per-revenue-account breakdown for the period.
    Total must match Income Statement's Revenue exactly.
    """
    end_date = end_date or date.today()
    accounts = Account.query.filter_by(
        company_id=company_id, type=AccountType.REVENUE, is_active=True
    ).order_by(Account.code).all()
    rows = []
    total = 0.0
    for acc in accounts:
        d, c = _account_balance(acc.id, start_date=start_date, end_date=end_date)
        bal = _signed_balance(acc, d, c)
        if abs(bal) < 0.01:
            continue
        rows.append({"code": acc.code, "name": acc.name_ar or acc.name, "balance": bal})
        total += bal
    return {"rows": rows, "total": total, "start_date": start_date, "end_date": end_date}


def expenses_summary(company_id, start_date=None, end_date=None):
    """Per-expense-account breakdown for the period.
    Total must match Income Statement's Expense exactly.
    Each row includes the underlying journal entry ids for drill-down.
    """
    end_date = end_date or date.today()
    accounts = Account.query.filter_by(
        company_id=company_id, type=AccountType.EXPENSE, is_active=True
    ).order_by(Account.code).all()
    rows = []
    total = 0.0
    for acc in accounts:
        d, c = _account_balance(acc.id, start_date=start_date, end_date=end_date)
        bal = _signed_balance(acc, d, c)
        if abs(bal) < 0.01:
            continue

        entry_q = db.session.query(JournalEntry.id).join(JournalLine).filter(
            JournalLine.account_id == acc.id,
            JournalEntry.is_active.is_(True),
        )
        if start_date:
            entry_q = entry_q.filter(JournalEntry.date >= start_date)
        if end_date:
            entry_q = entry_q.filter(JournalEntry.date <= end_date)
        entry_ids = [e[0] for e in entry_q.distinct().all()]

        rows.append({
            "id": acc.id,
            "code": acc.code, "name": acc.name_ar or acc.name,
            "balance": bal, "entry_ids": entry_ids, "entry_count": len(entry_ids),
        })
        total += bal
    return {"rows": rows, "total": total, "start_date": start_date, "end_date": end_date}


def income_statement_compared(company_id, start_date, end_date):
    """Full P&L with same period from previous year side-by-side."""
    current = income_statement(company_id, start_date=start_date, end_date=end_date)

    # Compute prior period: same span shifted back one year. Handle Feb-29 leap-year edge case.
    def _shift_year(d, years):
        try:
            return d.replace(year=d.year + years)
        except ValueError:
            # Feb 29 in non-leap year → fall back to Feb 28
            return d.replace(year=d.year + years, day=28)

    if start_date and end_date:
        prior_start = _shift_year(start_date, -1)
        prior_end = _shift_year(end_date, -1)
    else:
        prior_start = prior_end = None
    prior = income_statement(company_id, start_date=prior_start, end_date=prior_end)

    return {
        "current": current,
        "prior": prior,
        "delta_revenue": current["total_revenue"] - prior["total_revenue"],
        "delta_expense": current["total_expense"] - prior["total_expense"],
        "delta_net": current["net_income"] - prior["net_income"],
        "start_date": start_date, "end_date": end_date,
        "prior_start": prior_start, "prior_end": prior_end,
    }


def ap_aging_report(company_id, as_of=None):
    """Vendor aging — bills outstanding bucketed by days overdue.
    Total must match the Accounts Payable (2110) balance.
    """
    from app.models import VendorBill, VendorBillStatus, Vendor
    as_of = as_of or date.today()
    vendors = Vendor.query.filter_by(company_id=company_id, is_active=True).all()
    rows = []
    totals = {"current": 0.0, "d30": 0.0, "d60": 0.0, "d90": 0.0, "d90plus": 0.0, "total": 0.0}
    for v in vendors:
        buckets = {"current": 0.0, "d30": 0.0, "d60": 0.0, "d90": 0.0, "d90plus": 0.0}
        for bill in v.bills:
            if bill.status in (VendorBillStatus.PAID, VendorBillStatus.CANCELLED, VendorBillStatus.DRAFT):
                continue
            bal = bill.balance
            if bal <= 0.01:
                continue
            days_overdue = (as_of - bill.due_date).days
            if days_overdue <= 0:
                buckets["current"] += bal
            elif days_overdue <= 30:
                buckets["d30"] += bal
            elif days_overdue <= 60:
                buckets["d60"] += bal
            elif days_overdue <= 90:
                buckets["d90"] += bal
            else:
                buckets["d90plus"] += bal
        total = sum(buckets.values())
        if total > 0.01:
            rows.append({"vendor": v.name, "vendor_id": v.id, **buckets, "total": total})
            for k in buckets:
                totals[k] += buckets[k]
            totals["total"] += total
    return {"rows": rows, "totals": totals, "as_of": as_of}


def vat_report(company_id, start_date=None, end_date=None):
    """VAT report ready for government submission.
    VAT Collected = activity in 2120 (VAT Payable from sales invoices) credits − debits
    VAT Paid = activity in 5xxx tax-recoverable account (or via vendor bill taxes recorded on AP side)

    For MVP simplicity we read the VAT Payable account (2120) movements:
      - Credits = VAT collected from customers
      - Debits = VAT paid to suppliers (when applicable) or refunded
      Net = Credits − Debits
    """
    end_date = end_date or date.today()
    vat_account = Account.query.filter_by(company_id=company_id, code="2120").first()
    if not vat_account:
        return {"collected": 0, "paid": 0, "net": 0,
                "start_date": start_date, "end_date": end_date}
    d, c = _account_balance(vat_account.id, start_date=start_date, end_date=end_date)
    return {
        "collected": c,
        "paid": d,
        "net": c - d,   # positive = owed to government; negative = refund due
        "start_date": start_date, "end_date": end_date,
    }


def payroll_summary_report(company_id, year=None, month=None):
    """Monthly payroll summary across one or many periods."""
    from app.models import PayrollRun, PayrollLine
    q = PayrollRun.query.filter_by(company_id=company_id)
    if year:
        q = q.filter_by(period_year=year)
    if month:
        q = q.filter_by(period_month=month)
    runs = q.order_by(PayrollRun.period_year.desc(), PayrollRun.period_month.desc()).all()

    rows = []
    totals = {"basic": 0.0, "allowances": 0.0, "overtime": 0.0, "bonus": 0.0,
              "deductions": 0.0, "net": 0.0, "count": 0}
    for run in runs:
        for line in run.lines:
            deduct_total = float(line.deductions or 0) + float(line.absence_deduction or 0) + \
                           float(line.late_deduction or 0) + float(line.advance_deduction or 0)
            row = {
                "period": f"{run.period_month:02d}/{run.period_year}",
                "run_number": run.number,
                "employee": line.employee.name,
                "employee_number": line.employee.employee_number,
                "basic": float(line.basic), "allowances": float(line.allowances),
                "overtime": float(line.overtime), "bonus": float(line.bonus),
                "deductions": deduct_total, "net": float(line.net),
            }
            rows.append(row)
            totals["basic"] += row["basic"]
            totals["allowances"] += row["allowances"]
            totals["overtime"] += row["overtime"]
            totals["bonus"] += row["bonus"]
            totals["deductions"] += row["deductions"]
            totals["net"] += row["net"]
            totals["count"] += 1
    return {"rows": rows, "totals": totals, "year": year, "month": month}


def fixed_assets_report(company_id):
    """Full fixed assets inventory — total NBV must match the asset accounts on the balance sheet."""
    from app.models import FixedAsset
    assets = FixedAsset.query.filter_by(company_id=company_id, is_disposed=False).order_by(FixedAsset.created_at).all()
    rows = []
    totals = {"cost": 0.0, "annual_dep": 0.0, "accumulated_dep": 0.0, "nbv": 0.0}
    for a in assets:
        row = {
            "id": a.id,
            "name": a.name,
            "purchase_date": a.purchase_date,
            "useful_life_years": a.useful_life_years,
            "vendor": a.vendor.name if a.vendor else None,
            "cost": float(a.cost),
            "salvage_value": float(a.salvage_value or 0),
            "annual_dep": a.annual_depreciation,
            "monthly_dep": a.monthly_depreciation,
            "accumulated_dep": float(a.accumulated_depreciation or 0),
            "nbv": a.net_book_value,
            "account_code": a.account.code if a.account else "",
            "account_name": (a.account.name_ar or a.account.name) if a.account else "",
        }
        rows.append(row)
        totals["cost"] += row["cost"]
        totals["annual_dep"] += row["annual_dep"]
        totals["accumulated_dep"] += row["accumulated_dep"]
        totals["nbv"] += row["nbv"]
    return {"rows": rows, "totals": totals}


def aging_report(company_id, as_of=None):
    """Customer aging report: 0-30, 31-60, 61-90, 90+ days overdue."""
    from app.models import Invoice, InvoiceStatus, Customer
    as_of = as_of or date.today()
    customers = Customer.query.filter_by(company_id=company_id, is_active=True).all()
    rows = []
    totals = {"current": 0.0, "d30": 0.0, "d60": 0.0, "d90": 0.0, "d90plus": 0.0, "total": 0.0}
    for c in customers:
        buckets = {"current": 0.0, "d30": 0.0, "d60": 0.0, "d90": 0.0, "d90plus": 0.0}
        for inv in c.invoices:
            if inv.status in (InvoiceStatus.PAID, InvoiceStatus.CANCELLED, InvoiceStatus.REFUNDED):
                continue
            bal = inv.balance
            if bal <= 0.01:
                continue
            days_overdue = (as_of - inv.due_date).days
            if days_overdue <= 0:
                buckets["current"] += bal
            elif days_overdue <= 30:
                buckets["d30"] += bal
            elif days_overdue <= 60:
                buckets["d60"] += bal
            elif days_overdue <= 90:
                buckets["d90"] += bal
            else:
                buckets["d90plus"] += bal
        total = sum(buckets.values())
        if total > 0.01:
            rows.append({"customer": c.name, "customer_id": c.id, **buckets, "total": total})
            for k in buckets:
                totals[k] += buckets[k]
            totals["total"] += total
    return {"rows": rows, "totals": totals, "as_of": as_of}


def dashboard_metrics(company_id):
    """Key metrics for the dashboard."""
    from app.models import Invoice, InvoiceStatus
    today = date.today()
    start_month = today.replace(day=1)
    prev_month_end = start_month.replace(day=1)
    from calendar import monthrange
    prev_month_end = (start_month.replace(day=1).replace(day=1))
    # Just compute current month
    inc = income_statement(company_id, start_date=start_month, end_date=today)
    bs = balance_sheet(company_id, as_of=today)
    cash_accounts = Account.query.filter(
        Account.company_id == company_id, Account.code.in_(["1110", "1120"])
    ).all()
    cash_position = sum(float(a.balance) for a in cash_accounts)

    unpaid = Invoice.query.filter(
        Invoice.company_id == company_id,
        Invoice.status.in_([InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.OVERDUE])
    ).all()
    unpaid_total = sum(i.balance for i in unpaid)
    overdue = [i for i in unpaid if i.due_date < today]
    overdue_total = sum(i.balance for i in overdue)

    ap_account = Account.query.filter_by(company_id=company_id, code="2110").first()
    accounts_payable = float(ap_account.balance) if ap_account else 0.0

    total_assets = bs["totals"]["assets"]
    total_liab = bs["totals"]["liabilities"]
    total_equity = bs["totals"]["equity"]
    debt_to_equity = (total_liab / total_equity) if total_equity > 0.01 else 0.0

    current_assets_codes = ["1110", "1120", "1130", "1300", "1150"]
    current_liab_codes = ["2110", "2120", "2130", "2140"]
    current_assets = sum(
        float(a.balance) for a in Account.query.filter(
            Account.company_id == company_id, Account.code.in_(current_assets_codes)
        ).all()
    )
    current_liab = sum(
        float(a.balance) for a in Account.query.filter(
            Account.company_id == company_id, Account.code.in_(current_liab_codes)
        ).all()
    )
    current_ratio = (current_assets / current_liab) if current_liab > 0.01 else 0.0

    return {
        "total_revenue": inc["total_revenue"],
        "total_expenses": inc["total_expense"],
        "net_profit": inc["net_income"],
        "cash_position": cash_position,
        "unpaid_invoices": {"count": len(unpaid), "total": unpaid_total},
        "overdue_invoices": {"count": len(overdue), "total": overdue_total},
        "accounts_payable": accounts_payable,
        "debt_to_equity": debt_to_equity,
        "current_ratio": current_ratio,
    }
