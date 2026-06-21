"""MARSOUD-COMM-01 — sales commission service.

Phase A: compute & record a commission row + post its GL entry on every
real customer payment. The commission is on the PRE-TAX TAXABLE SHARE
of the payment, not the gross — abdelhamid was explicit about this.

Formula:
    taxable_base = payment_amount × (invoice.subtotal ÷ invoice.total)

If the invoice has no tax (subtotal == total) the share is 1.0 and the
base equals the payment amount as expected.

Posting:
    Dr 5280 Sales Commissions Expense
    Cr 2150 Sales Commissions Payable

Returns the created SalesCommission row (or None when no commission
applies — customer has no sales_rep / commission_rate, etc.).
"""
import logging
from datetime import date
from app import db
from app.models import SalesCommission
from app.services.ledger import post_journal, get_account_by_code, LedgerError

logger = logging.getLogger("ledgeros.sales_commissions")


def _customer_for_invoice(invoice):
    cust = invoice.customer
    return cust if cust else None


def _commission_inputs(invoice):
    """Return (sales_rep_id, commission_rate) for the invoice's customer,
    or (None, None) if no commission should apply."""
    cust = _customer_for_invoice(invoice)
    if not cust:
        return None, None
    rep_id = getattr(cust, "sales_rep_id", None)
    rate = getattr(cust, "commission_rate", None)
    if not rep_id or rate is None:
        return None, None
    try:
        rate_f = float(rate)
    except (TypeError, ValueError):
        return None, None
    if rate_f <= 0:
        return None, None
    return rep_id, rate_f


def compute_taxable_base(invoice, payment_amount):
    """Pre-tax portion of the payment per abdelhamid's spec.

    payment_amount × (subtotal / total)

    Falls back to payment_amount when total <= 0 (degenerate case —
    shouldn't happen but defensive)."""
    total = float(invoice.total or 0)
    if total <= 0:
        return float(payment_amount)
    subtotal = float(invoice.subtotal or 0)
    return float(payment_amount) * (subtotal / total)


def record_commission_for_payment(invoice, payment, payment_amount,
                                  *, payment_date=None, created_by=None):
    """Hook called from record_payment(). Idempotent — does nothing
    when the customer has no sales rep configured."""
    rep_id, rate = _commission_inputs(invoice)
    if not rep_id or not rate:
        return None

    base = compute_taxable_base(invoice, payment_amount)
    if base <= 0:
        return None
    commission_amount = round(base * rate / 100, 4)
    if commission_amount <= 0:
        return None

    # Post Dr 5280 / Cr 2150 — wrapped so a missing account doesn't
    # crash the payment flow (we log + skip).
    exp_acct = get_account_by_code(invoice.company_id, "5280")
    liab_acct = get_account_by_code(invoice.company_id, "2150")
    if not exp_acct or not liab_acct:
        logger.warning(
            "[COMMISSION-NO-ACCT] company=%s missing 5280/2150 — "
            "commission row NOT created for invoice %s (rep=%s, base=%.2f). "
            "Run the migration ab_2e7c4a9d5f1 to seed the accounts.",
            invoice.company_id, invoice.number, rep_id, base,
        )
        return None

    pay_date = payment_date or date.today()
    try:
        entry = post_journal(
            company_id=invoice.company_id,
            description=(
                f"عمولة مبيعات — {invoice.customer.name if invoice.customer else 'زبون'} "
                f"— فاتورة #{invoice.number}"
            ),
            lines=[
                {"account_id": exp_acct.id, "debit": commission_amount, "credit": 0},
                {"account_id": liab_acct.id, "debit": 0, "credit": commission_amount},
            ],
            entry_date=pay_date,
            reference=f"COMM-{invoice.number}",
            currency=invoice.currency,
            created_by=created_by,
            source_type="sales_commission",
            source_id=invoice.id,
        )
    except LedgerError as e:
        # Don't propagate — payment must still succeed even if commission
        # posting trips on something. The commission row simply doesn't
        # get inserted.
        logger.exception(
            "[COMMISSION-POST-FAIL] %s — invoice=%s rep=%s", e,
            invoice.number, rep_id,
        )
        return None

    row = SalesCommission(
        company_id=invoice.company_id,
        sales_rep_id=rep_id,
        customer_id=invoice.customer_id,
        invoice_id=invoice.id,
        payment_id=payment.id if payment else None,
        taxable_base=base,
        amount=commission_amount,
        commission_rate=rate,
        period_year=pay_date.year,
        period_month=pay_date.month,
        status="UNPAID",
        journal_entry_id=entry.id,
        is_carry_forward=False,
    )
    db.session.add(row)
    db.session.flush()
    return row


# ─── Phase B: refund handling ──────────────────────────────────────
def _next_month(d):
    """Return (year, month) for the calendar month after d."""
    if d.month == 12:
        return d.year + 1, 1
    return d.year, d.month + 1


def _post_clawback_journal(invoice, amount, refund_date, created_by):
    """Reverse the commission posting: Dr 2150 / Cr 5280 for `amount`."""
    exp_acct = get_account_by_code(invoice.company_id, "5280")
    liab_acct = get_account_by_code(invoice.company_id, "2150")
    if not exp_acct or not liab_acct:
        logger.warning(
            "[COMMISSION-CLAWBACK-NO-ACCT] company=%s missing 5280/2150 — "
            "clawback NOT posted for invoice %s",
            invoice.company_id, invoice.number,
        )
        return None
    return post_journal(
        company_id=invoice.company_id,
        description=(
            f"عكس عمولة مبيعات (استرجاع) — "
            f"{invoice.customer.name if invoice.customer else 'زبون'} "
            f"— فاتورة #{invoice.number}"
        ),
        lines=[
            # reverse direction of the original posting
            {"account_id": liab_acct.id, "debit": amount, "credit": 0},
            {"account_id": exp_acct.id, "debit": 0, "credit": amount},
        ],
        entry_date=refund_date,
        reference=f"COMM-RFND-{invoice.number}",
        currency=invoice.currency,
        created_by=created_by,
        source_type="sales_commission_refund",
        source_id=invoice.id,
    )


def record_commission_refund(invoice, refund, refund_amount,
                              *, refund_date=None, created_by=None):
    """MARSOUD-COMM-01 Phase B — claw back commission on a refund.

    Two scenarios per abdelhamid's spec:
      a) commission row is still UNPAID → insert a reversing negative
         row in the CURRENT month so the rep's pending balance shrinks
         before payroll closes.
      b) commission row is already PAID (settled in a prior PayrollRun)
         → insert a negative carry-forward row keyed to the NEXT month,
         which payroll will subtract from the rep's next earnings. If
         next month doesn't cover it, the carry-forward continues until
         the rep has enough commission to absorb it.

    When the unpaid bucket partially covers the clawback, the remainder
    automatically flows to carry-forward. So a single refund can yield
    1 or 2 negative rows.
    """
    rep_id, rate = _commission_inputs(invoice)
    if not rep_id or not rate:
        return []
    base = compute_taxable_base(invoice, refund_amount)
    if base <= 0:
        return []
    clawback = round(base * rate / 100, 4)
    if clawback <= 0:
        return []

    refund_date = refund_date or date.today()

    # How much UNPAID commission has this invoice already generated?
    # Sum the positive amounts ONLY — negative rows (prior clawbacks)
    # already reduced the bucket and shouldn't double-count.
    existing_rows = SalesCommission.query.filter_by(
        invoice_id=invoice.id, status="UNPAID",
    ).all()
    unpaid_pos = sum(float(r.amount or 0) for r in existing_rows
                       if float(r.amount or 0) > 0)
    # And any earlier negative clawbacks (already deducted)
    unpaid_neg = sum(float(r.amount or 0) for r in existing_rows
                       if float(r.amount or 0) < 0)
    unpaid_net = unpaid_pos + unpaid_neg   # negatives are already negative
    if unpaid_net < 0:
        unpaid_net = 0.0   # don't go below zero in budget terms

    out_rows = []

    # ── A) reverse from the unpaid bucket first ─────────────────────
    take_from_unpaid = min(unpaid_net, clawback)
    if take_from_unpaid > 0:
        entry = _post_clawback_journal(invoice, take_from_unpaid,
                                         refund_date, created_by)
        row = SalesCommission(
            company_id=invoice.company_id,
            sales_rep_id=rep_id,
            customer_id=invoice.customer_id,
            invoice_id=invoice.id,
            payment_id=None,
            taxable_base=-round(take_from_unpaid * 100 / rate, 4),
            amount=-take_from_unpaid,
            commission_rate=rate,
            period_year=refund_date.year,
            period_month=refund_date.month,
            status="UNPAID",
            journal_entry_id=entry.id if entry else None,
            is_carry_forward=False,
        )
        db.session.add(row)
        db.session.flush()
        out_rows.append(row)
        clawback -= take_from_unpaid

    # ── B) remainder → carry-forward (charged to NEXT month) ────────
    if clawback > 0.0001:
        ny, nm = _next_month(refund_date)
        entry = _post_clawback_journal(invoice, clawback,
                                         refund_date, created_by)
        row = SalesCommission(
            company_id=invoice.company_id,
            sales_rep_id=rep_id,
            customer_id=invoice.customer_id,
            invoice_id=invoice.id,
            payment_id=None,
            taxable_base=-round(clawback * 100 / rate, 4),
            amount=-clawback,
            commission_rate=rate,
            period_year=ny,
            period_month=nm,
            status="UNPAID",
            journal_entry_id=entry.id if entry else None,
            is_carry_forward=True,
        )
        db.session.add(row)
        db.session.flush()
        out_rows.append(row)

    return out_rows


# ─── Phase C: payroll integration ──────────────────────────────────
def settle_commissions_for_employee(employee, run, *, period_year, period_month):
    """MARSOUD-COMM-01 Phase C — fold the employee's outstanding sales
    commissions into a payroll run.

    Finds the User linked to this Employee (via User.employee_id), sums
    every UNPAID SalesCommission row keyed to that user where the row's
    (period_year, period_month) is <= the run's period (so carry-forward
    rows from prior months are settled here too), and returns the net
    amount. The rows are flipped to PAID + linked to the run.

    Returns 0 when:
      - employee has no linked user
      - employee's user has no commission rows in scope

    The amount can be negative when carry-forward clawbacks exceed the
    rep's earned commission for the period. The caller (run_payroll)
    just adds this to the line.commissions field; payroll math handles
    the negative naturally."""
    from app.models import User
    user = User.query.filter_by(employee_id=employee.id).first()
    if not user:
        return 0.0, []

    rows = SalesCommission.query.filter(
        SalesCommission.sales_rep_id == user.id,
        SalesCommission.company_id == run.company_id,
        SalesCommission.status == "UNPAID",
        db.or_(
            SalesCommission.period_year < period_year,
            db.and_(
                SalesCommission.period_year == period_year,
                SalesCommission.period_month <= period_month,
            ),
        ),
    ).all()
    if not rows:
        return 0.0, []

    net = sum(float(r.amount or 0) for r in rows)

    # MARSOUD-COMM-01 #9 — multi-month rollover. If the running balance is
    # negative (clawbacks > earned commissions), DON'T touch the rep's
    # salary AND don't settle anything — leave every row UNPAID so the
    # chain continues into next month's run with any new earnings layered
    # on top. The original clawback journal already adjusted 5280/2150;
    # we just defer the salary effect until earnings absorb the deficit.
    if net < -0.001:
        return 0.0, []

    for r in rows:
        r.status = "PAID"
        r.payroll_run_id = run.id
    db.session.flush()
    return round(net, 2), rows


# ─── Reports ──────────────────────────────────────────────────────
def commission_report(company_id, *, from_date=None, to_date=None):
    """MARSOUD-COMM-01 Phase C — per-rep commission report. Returns a
    list of dicts: rep + their commission breakdown over the date range
    (or all time if dates are None). Filtered on the row's created_at.

    For each rep:
      unpaid_total   sum of unpaid amounts (positive + negative)
      paid_total     sum of paid amounts (positive + negative)
      row_count      total rows in scope
      rows           the SalesCommission rows themselves, for drill-down
    """
    from app.models import User
    q = SalesCommission.query.filter_by(company_id=company_id)
    if from_date:
        q = q.filter(SalesCommission.created_at >= from_date)
    if to_date:
        from datetime import datetime, timedelta
        eod = datetime.combine(to_date + timedelta(days=1),
                                datetime.min.time())
        q = q.filter(SalesCommission.created_at < eod)
    all_rows = q.order_by(SalesCommission.created_at.desc()).all()

    by_rep = {}
    for r in all_rows:
        bucket = by_rep.setdefault(r.sales_rep_id, {
            "sales_rep_id": r.sales_rep_id,
            "sales_rep_name": None,
            "unpaid_total": 0.0, "paid_total": 0.0,
            "row_count": 0, "rows": [],
        })
        bucket["row_count"] += 1
        bucket["rows"].append(r)
        amt = float(r.amount or 0)
        if r.status == "PAID":
            bucket["paid_total"] += amt
        else:
            bucket["unpaid_total"] += amt
    # Resolve rep names in one go
    rep_ids = list(by_rep.keys())
    if rep_ids:
        users = User.query.filter(User.id.in_(rep_ids)).all()
        name_map = {u.id: u.full_name for u in users}
        for rep_id, bucket in by_rep.items():
            bucket["sales_rep_name"] = name_map.get(rep_id, f"User #{rep_id}")
            bucket["paid_total"] = round(bucket["paid_total"], 2)
            bucket["unpaid_total"] = round(bucket["unpaid_total"], 2)
    return sorted(by_rep.values(),
                   key=lambda b: b["unpaid_total"] + b["paid_total"],
                   reverse=True)
