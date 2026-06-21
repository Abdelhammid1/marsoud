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
