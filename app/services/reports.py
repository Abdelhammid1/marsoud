"""Financial report generators: Balance Sheet, Income Statement, Cash Flow."""
from datetime import date, datetime, time, timedelta
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
    that doesn't touch cash) are naturally excluded because they don't hit a cash
    account at all. If an entry that touches cash is marked NONCASH (rare — only when
    the user overrides or an operation declares it), it's still excluded.

    MARSOUD-OPS-FOUNDATION (2026-08-05) — this used to resolve cash as
    `code IN ("1110", "1120")`. The COA rebuild made 1120 «البنوك» a
    non-postable HEADER, and post_journal refuses lines on headers, so no
    journal line could ever hit it: **every bank movement was invisible in
    this statement** and it silently reported the cash box alone. The real
    bank accounts are the leaves beneath it (1121-1125, plus any the
    company added itself).

    It now walks the postable descendants of both roots, reusing the same
    helper the money picker uses, so "which accounts are cash" is answered
    in one place and a company that adds a sixth bank is covered with no
    code change.
    """
    end_date = end_date or date.today()
    from app.services.ledger import cash_account_ids
    cash_ids = cash_account_ids(company_id)

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
            # MARSOUD-VBILL-REFUND-STATUS — this is an EXCLUSION list, so
            # a newly added status is counted as payable unless named
            # here. A fully refunded bill owes the vendor nothing (its
            # balance only looks non-zero because paid_amount dropped
            # when the cash came back), so it must not age.
            # PARTIALLY_REFUNDED stays in, at its real balance.
            if bill.status in (VendorBillStatus.PAID,
                                VendorBillStatus.CANCELLED,
                                VendorBillStatus.DRAFT,
                                VendorBillStatus.REFUNDED):
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

    MARSOUD-COA-REBUILD — output and input VAT now live in separate
    accounts, so the report adds:
      - Output VAT (2120, liability)  — collected from customers
      - Input VAT  (1280, asset)      — paid to suppliers, recoverable
    and reports the net (output − input) which is what's owed to (or
    refundable from) the tax authority.

    Returns the same {collected, paid, net, …} shape as before so
    existing templates + KPIs don't break, plus a few extra keys for
    the new split.
    """
    end_date = end_date or date.today()
    output_acc = Account.query.filter_by(company_id=company_id, code="2120").first()
    input_acc = Account.query.filter_by(company_id=company_id, code="1280").first()

    # Output (liability, normal credit): credits − debits = net collected.
    if output_acc:
        od, oc = _account_balance(
            output_acc.id, start_date=start_date, end_date=end_date)
        output_net = oc - od
    else:
        output_net = 0.0

    # Input (asset, normal debit): debits − credits = net recoverable.
    if input_acc:
        id_d, id_c = _account_balance(
            input_acc.id, start_date=start_date, end_date=end_date)
        input_net = id_d - id_c
    else:
        input_net = 0.0

    return {
        "collected": output_net,   # kept for backward compatibility
        "paid": input_net,         # kept for backward compatibility
        "net": output_net - input_net,
        "output_vat": output_net,
        "input_vat": input_net,
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
    """Full fixed assets inventory — total active NBV must match the
    asset accounts on the balance sheet.

    MARSOUD-ASSET-DISPOSAL-01 (2026-08-07) — return shape gained a
    `disposed` section alongside the active one. The template splits
    on it; the accountant agent + dashboard KPI keep reading the
    top-level `rows`/`totals` (active only) for back-compat."""
    from app.models import FixedAsset
    all_assets = FixedAsset.query.filter_by(
        company_id=company_id).order_by(FixedAsset.created_at).all()

    def _row(a, *, include_disposal=False):
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
        if include_disposal:
            row.update({
                "disposal_date": (a.disposal_date.isoformat()
                                  if a.disposal_date else None),
                "disposal_reason": (a.disposal_reason.value
                                    if a.disposal_reason else None),
                "disposal_reason_ar": (a.disposal_reason.label_ar
                                       if a.disposal_reason else None),
                "disposal_proceeds": float(a.disposal_proceeds or 0),
                "disposal_note": a.disposal_note,
                "disposal_entry_id": a.disposal_journal_entry_id,
            })
        return row

    def _totals(rows):
        t = {"cost": 0.0, "annual_dep": 0.0,
             "accumulated_dep": 0.0, "nbv": 0.0}
        for r in rows:
            t["cost"] += r["cost"]
            t["annual_dep"] += r["annual_dep"]
            t["accumulated_dep"] += r["accumulated_dep"]
            t["nbv"] += r["nbv"]
        return t

    active_rows = [_row(a) for a in all_assets if not a.is_disposed]
    disposed_rows = [_row(a, include_disposal=True)
                     for a in all_assets if a.is_disposed]

    active_totals = _totals(active_rows)
    disposed_totals = _totals(disposed_rows)
    # Disposed rows also carry a proceeds total the active side
    # doesn't have.
    disposed_totals["proceeds"] = round(
        sum(r["disposal_proceeds"] for r in disposed_rows), 2)

    # Back-compat top-level keys — accountant agent's list_fixed_assets
    # + dashboard KPI read rows/totals by name. Additive: adding
    # `active`/`disposed` alongside doesn't break them.
    return {
        "rows": active_rows,
        "totals": active_totals,
        "active": {"rows": active_rows, "totals": active_totals},
        "disposed": {"rows": disposed_rows, "totals": disposed_totals},
    }


def aging_report(company_id, as_of=None):
    """Customer aging report: 0-30, 31-60, 61-90, 90+ days overdue."""
    from app.models import Invoice, InvoiceStatus, Customer
    from app.models.invoice import NON_RECEIVABLE_STATUSES
    as_of = as_of or date.today()
    customers = Customer.query.filter_by(company_id=company_id, is_active=True).all()
    rows = []
    totals = {"current": 0.0, "d30": 0.0, "d60": 0.0, "d90": 0.0, "d90plus": 0.0, "total": 0.0}
    for c in customers:
        buckets = {"current": 0.0, "d30": 0.0, "d60": 0.0, "d90": 0.0, "d90plus": 0.0}
        for inv in c.invoices:
            # MARSOUD-AR-AGING-VOIDED (Batch 8 Ticket 1, 2026-07-30) —
            # VOIDED invoices already have a reversal JE so their
            # ledger balance is zero. Excluding them here keeps the
            # aging report consistent with the trial balance / 1130
            # account balance shown at the bottom of the report.
            #
            # MARSOUD-OPS-FOUNDATION §5.3 (2026-08-05) — WRITTEN_OFF added
            # WITH the status, not after it. This is an EXCLUSION list: a
            # written-off invoice keeps its balance (the debt was
            # forgiven, not paid), so leaving it out of this tuple would
            # age it forever and the report would keep claiming money we
            # have already given up on. See audit_ops_foundation for the
            # check that every status is either excluded here or aged on
            # purpose.
            if inv.status in NON_RECEIVABLE_STATUSES:
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


def open_custody_report(company_id, as_of=None):
    """MARSOUD-CASH-CUSTODY-01 (2026-08-07, slice 3) — every
    cash custody that hasn't been fully settled yet, with days-
    open and an overdue flag.

    Mirrors aging_report's shape (rows + totals + as_of) so the
    /reports index tile behaves the same as AR/AP aging.

    A row shows up when custody.status in (ISSUED,
    PARTIALLY_SETTLED). SETTLED + CANCELLED are done deals — off
    the report. amount_pending is the truthy balance still
    floating: amount_issued - amount_settled - amount_returned.
    """
    from app.models import CashCustody, CustodyStatus
    as_of = as_of or date.today()
    q = CashCustody.query.filter(
        CashCustody.company_id == company_id,
        CashCustody.status.in_((CustodyStatus.ISSUED,
                                CustodyStatus.PARTIALLY_SETTLED)),
    ).order_by(CashCustody.issued_on.asc())
    rows = []
    totals = {"issued": 0.0, "settled": 0.0,
              "returned": 0.0, "pending": 0.0,
              "overdue_count": 0}
    for c in q.all():
        days_open = (as_of - c.issued_on).days if c.issued_on else 0
        pending = float(c.amount_pending or 0)
        is_overdue = bool(
            c.settlement_due_date
            and c.settlement_due_date < as_of)
        rows.append({
            "custody_id": c.id,
            "holder_type": c.holder_type.value,
            "holder_name": c.holder_name,
            "purpose": c.purpose,
            "issued_on": c.issued_on.isoformat() if c.issued_on else None,
            "amount_issued": float(c.amount_issued or 0),
            "amount_settled": float(c.amount_settled or 0),
            "amount_returned": float(c.amount_returned or 0),
            "amount_pending": pending,
            "days_open": days_open,
            "settlement_due_date": (c.settlement_due_date.isoformat()
                                    if c.settlement_due_date else None),
            "is_overdue": is_overdue,
            "status": c.status.value,
        })
        totals["issued"] += float(c.amount_issued or 0)
        totals["settled"] += float(c.amount_settled or 0)
        totals["returned"] += float(c.amount_returned or 0)
        totals["pending"] += pending
        if is_overdue:
            totals["overdue_count"] += 1
    return {"rows": rows, "totals": totals, "as_of": as_of}


def _cash_custody_metrics(company_id):
    """MARSOUD-CASH-CUSTODY-01 (slice 3, 2026-08-07) — one query
    for the dashboard tile. Returns the two counts the ops tile
    keys off:
        cash_custody_open      — ISSUED + PARTIALLY_SETTLED total
        cash_custody_overdue   — subset past settlement_due_date

    Wrapped in try so a company on a plan without the cash_custody
    module (tables not on the schema yet, or the model file failed
    to import) doesn't 500 the whole dashboard — 0 is safe."""
    try:
        from app.models import CashCustody, CustodyStatus
        from datetime import date as _date
        today = _date.today()
        open_rows = CashCustody.query.filter(
            CashCustody.company_id == company_id,
            CashCustody.status.in_((CustodyStatus.ISSUED,
                                    CustodyStatus.PARTIALLY_SETTLED)),
        ).all()
        return {
            "cash_custody_open": len(open_rows),
            "cash_custody_overdue": sum(
                1 for c in open_rows
                if c.settlement_due_date
                and c.settlement_due_date < today),
        }
    except Exception:
        return {"cash_custody_open": 0,
                "cash_custody_overdue": 0}


# ─── MARSOUD-DASHBOARD-COVERAGE-01 (2026-08-08) — ops metric
# helpers for the four new top-level tiles (HR, vendors, products,
# projects). Same shape as _cash_custody_metrics / _item_custody_
# metrics: private, try/except-wrapped so a plan without the module
# falls back to zeros rather than 500-ing the dashboard.

def _hr_metrics(company_id):
    """{hr_employees_active, hr_expiring_contracts} — active head-
    count + subset whose contract expires within 30 days."""
    try:
        from app.models import Employee, EmployeeStatus
        from datetime import date as _date, timedelta as _td
        today = _date.today()
        cutoff = today + _td(days=30)
        active = Employee.query.filter(
            Employee.company_id == company_id,
            Employee.status == EmployeeStatus.ACTIVE,
        ).all()
        return {
            "hr_employees_active": len(active),
            "hr_expiring_contracts": sum(
                1 for e in active
                if e.contract_end_date
                and today <= e.contract_end_date <= cutoff),
        }
    except Exception:
        return {"hr_employees_active": 0,
                "hr_expiring_contracts": 0}


def _vendors_metrics(company_id):
    """{vendors_count, vendors_with_balance} — active vendors +
    subset that currently have an outstanding AP balance."""
    try:
        from app.models import Vendor
        vendors = Vendor.query.filter_by(
            company_id=company_id, is_active=True).all()
        with_balance = 0
        for v in vendors:
            # Vendor.balance is a hybrid/computed on the model; if it
            # isn't there we fall back to the party sub-account lookup
            # via getattr so a schema shape drift doesn't 500.
            bal = 0
            try:
                bal = float(getattr(v, "balance", 0) or 0)
            except Exception:
                bal = 0
            if abs(bal) > 0.01:
                with_balance += 1
        return {
            "vendors_count": len(vendors),
            "vendors_with_balance": with_balance,
        }
    except Exception:
        return {"vendors_count": 0, "vendors_with_balance": 0}


def _products_metrics(company_id):
    """{products_count, products_missing_price} — active products/
    services + subset whose default_price is NULL or 0 (they can't
    be sold from POS until priced)."""
    try:
        from app.models import Product
        products = Product.query.filter_by(
            company_id=company_id, is_active=True).all()
        missing = sum(
            1 for p in products
            if not p.default_price or float(p.default_price) <= 0)
        return {
            "products_count": len(products),
            "products_missing_price": missing,
        }
    except Exception:
        return {"products_count": 0, "products_missing_price": 0}


def _projects_metrics(company_id):
    """{projects_open, projects_overdue} — projects in a non-terminal
    status + subset whose end_date is in the past. CLOSED and
    DELIVERED count as terminal here."""
    try:
        from app.models import Project, ProjectStatus
        from datetime import date as _date
        today = _date.today()
        terminal = (ProjectStatus.CLOSED, ProjectStatus.DELIVERED)
        open_projects = Project.query.filter(
            Project.company_id == company_id,
            Project.status.notin_(terminal),
        ).all()
        return {
            "projects_open": len(open_projects),
            "projects_overdue": sum(
                1 for p in open_projects
                if p.end_date and p.end_date < today),
        }
    except Exception:
        return {"projects_open": 0, "projects_overdue": 0}


def _month_range(year, month):
    """Return (first_of_month, last_of_month) date objects."""
    from calendar import monthrange
    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])
    return first, last


def _shift_month(d, delta):
    """Return the first-of-month date `delta` months before/after d."""
    m = d.month + delta
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    return date(y, m, 1)


def _arabic_month(d):
    """Arabic month name for a date object."""
    return [
        "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
        "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
    ][d.month - 1]


def _initials_for(name):
    """Two-letter Arabic initials from a full name."""
    if not name:
        return "؟"
    parts = name.strip().split()
    if len(parts) == 1:
        return parts[0][:2]
    return parts[0][:1] + parts[-1][:1]


def _item_custody_metrics(company_id):
    """MARSOUD-ITEM-CUSTODY-01 (dashboard tile, 2026-08-07) — the two
    keys the ops tile reads:
        item_custody_active   — count of ACTIVE ItemCustody rows
        item_custody_pending  — pending item-request approvals
                                 (owner sees a red nudge when > 0)
    Wrapped in try so a company on a plan without the item_custody
    module doesn't 500 the whole dashboard."""
    try:
        from app.models import (ItemCustody, ItemCustodyStatus,
                                ItemCustodyRequest,
                                ItemCustodyRequestStatus)
        active = ItemCustody.query.filter(
            ItemCustody.company_id == company_id,
            ItemCustody.status == ItemCustodyStatus.ACTIVE,
        ).count()
        pending = ItemCustodyRequest.query.filter(
            ItemCustodyRequest.company_id == company_id,
            ItemCustodyRequest.status == ItemCustodyRequestStatus.PENDING,
        ).count()
        return {
            "item_custody_active": active,
            "item_custody_pending": pending,
        }
    except Exception:
        return {"item_custody_active": 0, "item_custody_pending": 0}


def _account_balance_as_of(company_id, account_codes, as_of_date):
    """Sum debit−credit on the given accounts up to and including
    `as_of_date`. Used for historical sparkline points."""
    from app.models import Account, JournalEntry, JournalLine
    from app import db as _db
    accounts = Account.query.filter(
        Account.company_id == company_id,
        Account.code.in_(account_codes),
    ).all()
    if not accounts:
        return 0.0
    account_ids = [a.id for a in accounts]
    rows = _db.session.query(
        _db.func.sum(JournalLine.debit - JournalLine.credit)
    ).join(JournalEntry).filter(
        JournalEntry.company_id == company_id,
        JournalEntry.is_active.is_(True),
        JournalEntry.date <= as_of_date,
        JournalLine.account_id.in_(account_ids),
    ).scalar()
    return float(rows or 0)


def _ar_balance_as_of(company_id, as_of_date):
    """Sum of unpaid invoice balances issued on or before as_of_date.
    Approximation for sparkline use: ignores partial payments that
    happened after as_of_date (close enough for trend visualisation)."""
    from app.models import Invoice, InvoiceStatus
    invs = Invoice.query.filter(
        Invoice.company_id == company_id,
        Invoice.issue_date <= as_of_date,
        # MARSOUD-AR-AGING-VOIDED (Batch 8 Ticket 1, 2026-07-30) —
        # VOIDED excluded for the same reason as in aging_report().
        ~Invoice.status.in_((InvoiceStatus.PAID, InvoiceStatus.CANCELLED,
                              InvoiceStatus.REFUNDED, InvoiceStatus.DRAFT,
                              InvoiceStatus.VOIDED)),
    ).all()
    return sum(float(i.balance or 0) for i in invs)


def _next_ap_due_days(company_id):
    """Days until the next vendor-bill due date, or None if no pending bills."""
    from app.models import VendorBill, VendorBillStatus
    bills = VendorBill.query.filter(
        VendorBill.company_id == company_id,
        VendorBill.status.in_((VendorBillStatus.POSTED,
                                VendorBillStatus.PARTIALLY_PAID)),
    ).all()
    if not bills:
        return None
    today = date.today()
    due_dates = [b.due_date for b in bills if b.due_date and b.due_date >= today]
    if not due_dates:
        return 0  # everything is overdue
    return (min(due_dates) - today).days


def _pct_change(curr, prev):
    """Percentage delta of curr vs prev, rounded. Returns 0 for prev==0."""
    if not prev or abs(prev) < 0.001:
        return 0
    return int(round((curr - prev) / abs(prev) * 100))


def dashboard_metrics(company_id, period="month"):
    """MARSOUD-DASH-01 — Owner Dashboard data.

    Returns a deeply-nested dict that feeds every section of the new
    dashboard: financial-health KPIs (with 8-month sparklines + % change),
    operations cards, needs-attention panels, financial trend (6-month
    bars + expense breakdown), team performance leaderboards.

    `period` accepts "day" / "month" / "quarter" / "year" and shifts the
    KPI window + prior-period comparison. The 8-month sparklines and
    6-month trend bars stay month-scoped regardless — they're inherently
    monthly time series and shouldn't collapse with the header switch.

    Backward-compat: every key from the original return shape is still
    present so older callers (agent tools, existing dashboard fragments
    if any survived) don't break.
    """
    from app.models import (
        Invoice, InvoiceStatus, Lead, LeadStatus, Task, TaskStatus,
        Customer, ProductVariant, JournalAudit, User, Company,
        task_assignees,
    )
    from app.models.user import user_companies
    from app import db as _db

    today = date.today()
    # start_month is kept because the sparklines/trend explicitly need
    # a month-of-today floor. It is NOT the same as the KPI period start.
    start_month = today.replace(day=1)

    # Compute the KPI period + prior-period range for percent-change.
    period = period if period in ("day", "month", "quarter", "year") else "month"
    if period == "day":
        start_period = today
        prev_start = today - timedelta(days=1)
        prev_end = today - timedelta(days=1)
        period_label = "اليوم"
    elif period == "quarter":
        # Q1: Jan-Mar, Q2: Apr-Jun, Q3: Jul-Sep, Q4: Oct-Dec.
        q_first_month = ((today.month - 1) // 3) * 3 + 1
        start_period = date(today.year, q_first_month, 1)
        prev_start = _shift_month(start_period, -3)
        prev_end = start_period - timedelta(days=1)
        period_label = "الربع"
    elif period == "year":
        start_period = date(today.year, 1, 1)
        prev_start = date(today.year - 1, 1, 1)
        prev_end = date(today.year - 1, 12, 31)
        period_label = "السنة"
    else:  # "month"
        start_period = start_month
        prev_start = _shift_month(start_period, -1)
        prev_end = start_period - timedelta(days=1)
        period_label = "الشهر"

    # Backwards-compat aliases: the rest of the function still references
    # prev_month_start/prev_month_end — we now point them at the chosen
    # period's previous-window bounds so the % change and new-customers
    # comparison move together with the header selector.
    prev_month_start = prev_start
    prev_month_end = prev_end

    company = Company.query.get(company_id)
    currency = company.base_currency if company else "EGP"

    # ─── Current-period income statement (backwards-compat fields) ────
    inc = income_statement(company_id, start_date=start_period, end_date=today)
    inc_prev = income_statement(
        company_id, start_date=prev_start, end_date=prev_end,
    )

    # ─── Cash position (current) ─────────────────────────────────────
    # MARSOUD-OPS-FOUNDATION — same bug as the cash-flow statement: this
    # asked for codes 1110 + 1120, but 1120 is a non-postable header with
    # no journal lines of its own, so «السيولة المتاحة» on the dashboard
    # was reporting the cash box and none of the banks.
    from app.services.ledger import cash_accounts as _cash_accounts
    _cash_codes = [a.code for a in _cash_accounts(company_id)]
    cash_position = _account_balance_as_of(company_id, _cash_codes, today)
    cash_position_prev = _account_balance_as_of(
        company_id, _cash_codes, prev_month_end,
    )

    # ─── Invoices (open + overdue) ───────────────────────────────────
    unpaid = Invoice.query.filter(
        Invoice.company_id == company_id,
        Invoice.status.in_([InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID,
                            InvoiceStatus.OVERDUE]),
    ).all()
    unpaid_total = sum(float(i.balance or 0) for i in unpaid)
    overdue = [i for i in unpaid if i.due_date and i.due_date < today]
    overdue_total = sum(float(i.balance or 0) for i in overdue)
    ar_prev = _ar_balance_as_of(company_id, prev_month_end)

    # ─── AP (vendor bills payable) ───────────────────────────────────
    ap_account = Account.query.filter_by(
        company_id=company_id, code="2110",
    ).first()
    accounts_payable = float(ap_account.balance) if ap_account else 0.0
    ap_prev = _account_balance_as_of(company_id, ["2110"], prev_month_end)
    # AP balances live as credit-positive — store the magnitude for display.
    ap_prev = abs(ap_prev) if ap_prev else 0.0

    # ─── Ratios (backwards-compat) ───────────────────────────────────
    bs = balance_sheet(company_id, as_of=today)
    total_equity = bs["totals"]["equity"]
    total_liab = bs["totals"]["liabilities"]
    debt_to_equity = (total_liab / total_equity) if total_equity > 0.01 else 0.0
    # MARSOUD-COA-REBUILD — 1120/1130/2110/2130 are now header accounts
    # whose .balance walks their subtree, so adding more leaf codes here
    # isn't needed. 1280 (input VAT) is added on the asset side; 2125
    # (net VAT payable) is added on the liability side.
    current_assets = sum(
        float(a.balance) for a in Account.query.filter(
            Account.company_id == company_id,
            Account.code.in_(["1110", "1120", "1130", "1280", "1300", "1150"]),
        ).all()
    )
    current_liab = sum(
        float(a.balance) for a in Account.query.filter(
            Account.company_id == company_id,
            Account.code.in_(["2110", "2120", "2125", "2130", "2140"]),
        ).all()
    )
    current_ratio = (current_assets / current_liab) if current_liab > 0.01 else 0.0

    # ─── 8-month sparklines for the 4 main KPIs ──────────────────────
    spark_months = [_shift_month(start_month, -i) for i in range(7, -1, -1)]
    # 1120 is now a header — its descendants are 1121-1125. Including
    # them ensures the cash sparkline keeps reading bank balances.
    cash_spark = [
        round(_account_balance_as_of(
            company_id,
            ["1110", "1121", "1122", "1123", "1124", "1125"],
            _month_range(m.year, m.month)[1]), 2)
        for m in spark_months
    ]
    profit_spark = [
        round(income_statement(company_id,
                                start_date=m,
                                end_date=_month_range(m.year, m.month)[1])
                ["net_income"], 2)
        for m in spark_months
    ]
    ar_spark = [
        round(_ar_balance_as_of(company_id,
                                 _month_range(m.year, m.month)[1]), 2)
        for m in spark_months
    ]
    ap_spark = [
        round(abs(_account_balance_as_of(company_id, ["2110"],
                                          _month_range(m.year, m.month)[1])), 2)
        for m in spark_months
    ]

    # ─── 6-month revenue/expense trend (last 6 months) ───────────────
    trend_months = [_shift_month(start_month, -i) for i in range(5, -1, -1)]
    trend_labels = [_arabic_month(m) for m in trend_months]
    trend_revenue = []
    trend_expenses = []
    for m in trend_months:
        m_end = _month_range(m.year, m.month)[1]
        s = income_statement(company_id, start_date=m, end_date=m_end)
        trend_revenue.append(round(s["total_revenue"], 2))
        trend_expenses.append(round(s["total_expense"], 2))

    # ─── Current-period expense breakdown by account ──────────────────
    expense_rows = expenses_summary(company_id, start_date=start_period,
                                     end_date=today)
    # expenses_summary returns {"rows": [{"name", "amount", ...}], "total": X}
    exp_total = float(expense_rows.get("total") or 0)
    exp_palette = ["#159b54", "#2d6fb3", "#c47d10", "#6a52c4", "#0e9b86", "#d6ded8"]
    expense_breakdown = []
    sorted_exp = sorted(expense_rows.get("rows", []),
                         key=lambda r: -float(r.get("amount") or 0))
    top = sorted_exp[:5]
    other = sorted_exp[5:]
    for i, row in enumerate(top):
        amt = float(row.get("amount") or 0)
        if amt <= 0:
            continue
        expense_breakdown.append({
            "name": row.get("name") or "—",
            "amount": amt,
            "pct": round(amt / exp_total * 100, 1) if exp_total > 0 else 0,
            "color": exp_palette[i],
        })
    if other:
        other_amt = sum(float(r.get("amount") or 0) for r in other)
        if other_amt > 0:
            expense_breakdown.append({
                "name": "أخرى",
                "amount": other_amt,
                "pct": round(other_amt / exp_total * 100, 1) if exp_total > 0 else 0,
                "color": exp_palette[5],
            })

    # ─── Operations cards ───────────────────────────────────────────
    inventory_count = ProductVariant.query.filter_by(
        company_id=company_id, is_active=True,
    ).count()
    low_stock_variants = [
        v for v in ProductVariant.query.filter_by(
            company_id=company_id, is_active=True,
        ).all() if v.is_low_stock
    ]
    # MARSOUD dashboard fix (image #55): "active tasks" must reflect tasks
    # actively being worked on. BLOCKED is parked, DONE is finished — both
    # are excluded. Previously BLOCKED was counted, which made 10 active +
    # 1 blocked render as 11.
    _ACTIVE_STATUSES = (TaskStatus.TODO, TaskStatus.IN_PROGRESS,
                         TaskStatus.REVIEW)
    tasks_open = Task.query.filter(
        Task.company_id == company_id,
        Task.status.in_(_ACTIVE_STATUSES),
    ).count()
    tasks_overdue = sum(
        1 for t in Task.query.filter(
            Task.company_id == company_id,
            Task.status.in_(_ACTIVE_STATUSES),
        ).all()
        if t.is_overdue
    )
    # MARSOUD-CRM-NO-RESPONSE — parked leads aren't part of the
    # active pipeline either. Exclude them from the "open leads"
    # rollup that feeds owner-dashboard expected-value totals.
    open_lead_statuses = [
        s for s in LeadStatus
        if s not in (LeadStatus.WON, LeadStatus.LOST, LeadStatus.NO_RESPONSE)
    ]
    open_leads = Lead.query.filter(
        Lead.company_id == company_id,
        Lead.deleted_at.is_(None),
        Lead.status.in_(open_lead_statuses),
    ).all()
    leads_expected_total = sum(float(L.expected_value or 0) for L in open_leads)

    new_customers_count = Customer.query.filter(
        Customer.company_id == company_id,
        Customer.created_at >= datetime.combine(start_period, time.min),
    ).count()
    new_customers_prev = Customer.query.filter(
        Customer.company_id == company_id,
        Customer.created_at >= datetime.combine(prev_start, time.min),
        Customer.created_at < datetime.combine(start_period, time.min),
    ).count()

    # ─── Section 3: Needs Attention ─────────────────────────────────
    late_sorted = sorted(overdue, key=lambda i: i.due_date or today)[:3]
    late_invoices = [{
        "id": i.id,
        "number": i.number,
        "customer_name": (i.customer.name if i.customer else "—"),
        "customer_initials": _initials_for(i.customer.name if i.customer else None),
        "amount": float(i.balance or 0),
        "days_late": (today - i.due_date).days if i.due_date else 0,
        # MARSOUD-DASHBOARD-INVOICE-TITLE (Abdelhamid 2026-07-29) —
        # subtitle used to be just the invoice number. Users
        # couldn't tell invoices apart at a glance. Fallback chain:
        # invoice.notes (first 60 chars) → invoice.number.
        "title_for_display": (
            (i.notes.strip()[:60] if i.notes and i.notes.strip()
             else i.number)
        ),
    } for i in late_sorted]

    # MARSOUD-VBILL-OVERDUE-01 (2026-08-06) — the vendor-side mirror of
    # late_invoices. Merges (1) real vendor bills past due_date with
    # (2) recurring-bill forecasts whose date has passed but that the
    # cron has not yet materialised. The second half is belt-and-
    # suspenders: if cron fails on a given day, the row still surfaces
    # as red with an "اعمل الفاتورة" button instead of disappearing.
    # MARSOUD-DASHBOARD-VBILL-EMPTY-FIX — the panel is split into
    # two INDEPENDENT try blocks now. A bug in the recurring-bills
    # forecast used to blow up the whole thing via one shared
    # `except Exception: pass`, leaving the panel silently empty even
    # though real overdue bills existed. Two separate excepts + real
    # logging make that class of bug visible in prod logs.
    #
    # Filter details worth remembering:
    #   · status IN (OVERDUE | POSTED | PARTIALLY_PAID) — POSTED
    #     stays in because the OVERDUE cron might not have flipped
    #     it yet; the dashboard is the fallback.
    #   · balance > 0 was NOT filtered before; add it now so a bill
    #     whose status is stuck at OVERDUE but is actually fully
    #     paid (rare, but possible after a manual payment race)
    #     doesn't linger as a red row.
    late_vendor_bills = []
    late_vendor_total = 0.0
    import logging as _logging
    _log = _logging.getLogger("marsoud.dashboard")

    try:
        from app.models import VendorBill, VendorBillStatus

        real_overdue = VendorBill.query.filter(
            VendorBill.company_id == company_id,
            VendorBill.deleted_at.is_(None),
            VendorBill.status.in_([
                VendorBillStatus.OVERDUE,
                VendorBillStatus.POSTED,
                VendorBillStatus.PARTIALLY_PAID,
            ]),
            VendorBill.due_date < today,
        ).order_by(VendorBill.due_date.asc()).all()

        _log.info(
            "late_vendor_bills(real): company=%s today=%s found=%s",
            company_id, today, len(real_overdue))

        for b in real_overdue:
            amt = float(b.balance or 0)
            if amt <= 0:
                # Bill was paid off — skip so it doesn't stay red
                # forever if the status column drifted from the truth.
                continue
            days_late = (today - b.due_date).days if b.due_date else 0
            vendor_name = b.vendor.name if b.vendor else "—"
            title = (b.notes.strip()[:60] if b.notes and b.notes.strip()
                     else b.number)
            late_vendor_total += amt
            late_vendor_bills.append({
                "kind": "bill",
                "id": b.id,
                "number": b.number,
                "vendor_name": vendor_name,
                "vendor_initials": _initials_for(vendor_name),
                "amount": amt,
                "days_late": days_late,
                "title_for_display": title,
                "source_recurring_bill_id": None,
                "occurrence_date": None,
            })
    except Exception:
        # Log the traceback so a future prod run leaves breadcrumbs.
        _log.exception(
            "late_vendor_bills(real) failed for company=%s", company_id)

    try:
        from app.services.recurring_bills import unmaterialised_past_due
        forecast_rows = unmaterialised_past_due(company_id, as_of=today)
        _log.info(
            "late_vendor_bills(forecast): company=%s found=%s",
            company_id, len(forecast_rows))
        for row in forecast_rows:
            days_late = (today - row["date"]).days
            amt = float(row.get("amount") or 0)
            late_vendor_total += amt
            late_vendor_bills.append({
                "kind": "forecast",
                "id": None,
                "number": None,
                "vendor_name": row.get("vendor_name") or "—",
                "vendor_initials": _initials_for(row.get("vendor_name")),
                "amount": amt,
                "days_late": days_late,
                "title_for_display": (
                    row.get("template_label") or "توقع فاتورة دورية"),
                "source_recurring_bill_id": row["recurring_bill_id"],
                "occurrence_date": row["date"],
            })
    except Exception:
        _log.exception(
            "late_vendor_bills(forecast) failed for company=%s",
            company_id)

    # Most-overdue first (real + forecast unified).
    late_vendor_bills.sort(key=lambda r: r["days_late"], reverse=True)

    # Upcoming bills via the MARSOUD-65 forecast helper.
    upcoming_bills = []
    upcoming_total = 0.0
    try:
        from app.services.recurring_bills import get_due_within
        from app.models import VendorBill
        forecast_data = get_due_within(company_id, days=7)
        # MARSOUD-DASHBOARD-RECURRING-TITLE (Batch 6 Ticket 3,
        # 2026-07-29) — batch-load source VendorBills so we can
        # surface their notes/number for a distinguishable title
        # + link straight to the vendor bill instead of the
        # recurring-bills list.
        src_ids = {r.get("source_bill_id") for r in forecast_data.get("rows", [])
                    if r.get("source_bill_id")}
        src_bills = {b.id: b for b in
                      VendorBill.query.filter(
                          VendorBill.id.in_(src_ids)).all()} if src_ids else {}
        for row in forecast_data.get("rows", [])[:3]:
            days_until = (row["date"] - today).days
            vendor_name = row.get("vendor_name") or "—"
            src = src_bills.get(row.get("source_bill_id"))
            # Fallback chain for the title: source bill notes (60
            # chars) → vendor name → interval label. Same shape as
            # Batch 5 Ticket 3 for late_invoices.
            title = None
            if src and src.notes and src.notes.strip():
                title = src.notes.strip()[:60]
            if not title:
                title = vendor_name if vendor_name != "—" else (
                    row.get("template_label") or "فاتورة متكررة")
            upcoming_bills.append({
                "id": row.get("recurring_bill_id"),
                "source_bill_id": row.get("source_bill_id"),
                "label": row.get("template_label") or vendor_name,
                "title_for_display": title,
                "vendor_name": vendor_name,
                "vendor_initials": _initials_for(vendor_name),
                "amount": float(row.get("amount") or 0),
                "currency": row.get("currency") or currency,
                "days_until": days_until,
                "interval_label": "متكرر",
            })
        # Total in base currency (most installs use one currency).
        upcoming_total = sum(float(r.get("amount") or 0)
                              for r in forecast_data.get("rows", []))
    except Exception:
        pass

    # ─── Section 5: Team Performance ────────────────────────────────
    # The route gates this section on permission; we always compute it
    # for simplicity (cheap-enough queries).
    team_member_rows = _db.session.execute(
        user_companies.select().where(user_companies.c.company_id == company_id)
    ).fetchall()
    role_label_map = {}
    try:
        from app.services.permissions import ROLE_LABELS_AR
        role_label_map = ROLE_LABELS_AR
    except Exception:
        pass
    user_ids = [r.user_id for r in team_member_rows]
    user_map = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()}
    role_by_user = {r.user_id: role_label_map.get(r.role, r.role or "—")
                    for r in team_member_rows}

    # Most-active: count JournalAudit rows within the chosen period.
    audit_counts = dict(_db.session.query(
        JournalAudit.user_id, _db.func.count(JournalAudit.id),
    ).filter(
        JournalAudit.user_id.in_(user_ids),
        JournalAudit.created_at >= datetime.combine(start_period, time.min),
    ).group_by(JournalAudit.user_id).all())

    def _make_team_row(uid, count_val, extra=None):
        u = user_map.get(uid)
        if not u:
            return None
        return {
            "user_id": uid,
            "name": u.full_name or u.email,
            "role_ar": role_by_user.get(uid, "—"),
            "initials": _initials_for(u.full_name or u.email),
            "count": count_val,
            **(extra or {}),
        }

    team_most_active = sorted(
        [r for r in (_make_team_row(uid, c)
                     for uid, c in audit_counts.items()) if r],
        key=lambda r: -r["count"],
    )[:4]

    # Most tasks assigned: count open tasks per assignee (multi-assignee aware).
    task_count_query = _db.session.query(
        task_assignees.c.user_id, _db.func.count(Task.id),
    ).join(Task, Task.id == task_assignees.c.task_id).filter(
        Task.company_id == company_id,
        Task.status != TaskStatus.DONE,
        task_assignees.c.user_id.in_(user_ids),
    ).group_by(task_assignees.c.user_id).all()
    open_task_overdue = {}
    for t in Task.query.filter(Task.company_id == company_id,
                                 Task.status != TaskStatus.DONE).all():
        if not t.is_overdue:
            continue
        # Count this task for each of its assignees
        for uid_row in _db.session.execute(
            task_assignees.select().where(task_assignees.c.task_id == t.id)
        ).fetchall():
            open_task_overdue[uid_row.user_id] = \
                open_task_overdue.get(uid_row.user_id, 0) + 1

    team_most_tasks = sorted(
        [r for r in (_make_team_row(uid, c,
                                     {"overdue": open_task_overdue.get(uid, 0)})
                     for uid, c in task_count_query) if r],
        key=lambda r: -r["count"],
    )[:4]

    # Most leads: count open leads per assignee.
    lead_counts = dict(_db.session.query(
        Lead.assigned_to_id, _db.func.count(Lead.id),
    ).filter(
        Lead.company_id == company_id,
        Lead.deleted_at.is_(None),
        Lead.status.in_(open_lead_statuses),
        Lead.assigned_to_id.in_(user_ids),
    ).group_by(Lead.assigned_to_id).all())
    team_most_leads = sorted(
        [r for r in (_make_team_row(uid, c)
                     for uid, c in lead_counts.items()) if r],
        key=lambda r: -r["count"],
    )[:4]

    return {
        # ─── Back-compat scalars ─────────────────────────────────────
        "total_revenue": inc["total_revenue"],
        "total_expenses": inc["total_expense"],
        "net_profit": inc["net_income"],
        "cash_position": cash_position,
        "unpaid_invoices": {"count": len(unpaid), "total": unpaid_total},
        "overdue_invoices": {"count": len(overdue), "total": overdue_total},
        "accounts_payable": accounts_payable,
        "debt_to_equity": debt_to_equity,
        "current_ratio": current_ratio,

        # ─── DASH-01 ─────────────────────────────────────────────────
        "currency": currency,
        "period": period,
        "period_label": period_label,
        "kpis": {
            "liquidity": {
                "value": cash_position,
                "spark": cash_spark,
                "change_pct": _pct_change(cash_position, cash_position_prev),
            },
            "net_profit_month": {
                "value": inc["net_income"],
                "spark": profit_spark,
                "change_pct": _pct_change(inc["net_income"], inc_prev["net_income"]),
            },
            "ar_open": {
                "value": unpaid_total,
                "spark": ar_spark,
                "overdue_count": len(overdue),
                "change_pct": _pct_change(unpaid_total, ar_prev),
            },
            "ap_open": {
                "value": accounts_payable,
                "spark": ap_spark,
                "next_due_days": _next_ap_due_days(company_id),
                "change_pct": _pct_change(accounts_payable, ap_prev),
            },
        },
        "ops": {
            "inventory_count": inventory_count,
            "inventory_low_stock": len(low_stock_variants),
            "tasks_open": tasks_open,
            "tasks_overdue": tasks_overdue,
            "leads_open_count": len(open_leads),
            "leads_expected_total": leads_expected_total,
            "customers_new_month": new_customers_count,
            "customers_new_prev": new_customers_prev,
            "customers_delta": new_customers_count - new_customers_prev,
            # MARSOUD-CASH-CUSTODY-01 (slice 3, 2026-08-07) — backs
            # the dashboard's "العهد النقدية" tile. Open = ISSUED
            # or PARTIALLY_SETTLED; overdue subset drives the red
            # nudge tag. Wrapped in try so a company on a plan
            # without cash_custody doesn't 500 the whole dashboard
            # if the tables aren't there — 0 is safe.
            **_cash_custody_metrics(company_id),
            # MARSOUD-ITEM-CUSTODY-01 (dashboard tile, 2026-08-07)
            **_item_custody_metrics(company_id),
            # MARSOUD-DASHBOARD-COVERAGE-01 (2026-08-08) — four new
            # top-level ops tiles for HR / vendors / products /
            # projects. Same shape as the custody unpacks above.
            **_hr_metrics(company_id),
            **_vendors_metrics(company_id),
            **_products_metrics(company_id),
            **_projects_metrics(company_id),
        },
        "late_invoices": late_invoices,
        "late_invoices_total": overdue_total,
        "late_invoices_count": len(overdue),
        # MARSOUD-VBILL-OVERDUE-01 (2026-08-06)
        "late_vendor_bills": late_vendor_bills,
        "late_vendor_bills_total": late_vendor_total,
        "late_vendor_bills_count": len(late_vendor_bills),
        "upcoming_bills": upcoming_bills,
        "upcoming_bills_total": upcoming_total,
        "upcoming_bills_count": len(upcoming_bills),
        "trend_6m": {
            "labels": trend_labels,
            "revenue": trend_revenue,
            "expenses": trend_expenses,
        },
        "expense_breakdown": expense_breakdown,
        "team_most_active": team_most_active,
        "team_most_tasks": team_most_tasks,
        "team_most_leads": team_most_leads,
    }
