"""MARSOUD-CUSTOMER-DEPOSIT-01 (Abdelhamid 2026-07-24).

Advance-payment (deposit) service layer. Uses party_ar_account so
the credit lands in the customer's own sub-account under 1130 —
consistent with regular invoice / payment postings.
"""
from datetime import date, datetime
from decimal import Decimal
from app import db
from app.models import (
    CustomerDeposit, Customer, PaymentMethod,
    DEPOSIT_ACTIVE, DEPOSIT_APPLIED, DEPOSIT_REFUNDED,
)
from app.services.ledger import post_journal, LedgerError
from app.services.subsidiary import ensure_customer_account
from app.services.numbering import next_number


class DepositError(Exception):
    """User-visible deposit error (over-apply, no active balance, etc.)."""


def record_deposit(*, company_id, customer, amount, payment_method,
                    date_=None, notes=None, actor_id=None):
    """Create a CustomerDeposit + balanced JE.

    Dr <payment method account>  amount
       Cr <customer AR sub-account>  amount
    """
    if amount is None or Decimal(str(amount)) <= 0:
        raise DepositError("مبلغ العربون يجب أن يكون أكبر من صفر")
    if not payment_method:
        raise DepositError("طريقة الدفع مطلوبة")
    if not customer:
        raise DepositError("اختر العميل")
    amt = Decimal(str(amount))

    ar = ensure_customer_account(customer)
    if not ar:
        raise LedgerError("تعذر إنشاء الحساب الفرعي للعميل")

    doc_number = next_number(company_id, "DEPOSIT")
    deposit = CustomerDeposit(
        company_id=company_id,
        customer_id=customer.id,
        doc_number=doc_number,
        amount=amt,
        payment_method_id=payment_method.id,
        date=date_ or date.today(),
        status=DEPOSIT_ACTIVE,
        notes=notes,
        created_by_id=actor_id,
    )
    db.session.add(deposit); db.session.flush()

    entry = post_journal(
        company_id=company_id,
        description=(f"استلام عربون {doc_number} — {customer.name}"),
        lines=[
            {"account_id": payment_method.account_id,
             "debit": float(amt), "credit": 0,
             "memo": f"عربون {doc_number}"},
            {"account_id": ar.id,
             "debit": 0, "credit": float(amt),
             "memo": f"عربون من {customer.name}"},
        ],
        entry_date=deposit.date,
        reference=doc_number,
        currency=(customer.company.base_currency
                   if customer.company else "EGP"),
        created_by=actor_id,
        source_type="customer_deposit",
        source_id=deposit.id,
    )
    deposit.journal_entry_id = entry.id
    db.session.commit()
    return deposit


def active_deposits_for_customer(customer_id):
    return CustomerDeposit.query.filter_by(
        customer_id=customer_id,
        status=DEPOSIT_ACTIVE,
    ).order_by(CustomerDeposit.date.asc()).all()


def total_active_amount(customer_id):
    from sqlalchemy import func
    total = db.session.query(
        func.coalesce(func.sum(CustomerDeposit.amount), 0)
    ).filter(
        CustomerDeposit.customer_id == customer_id,
        CustomerDeposit.status == DEPOSIT_ACTIVE,
    ).scalar()
    return Decimal(str(total or 0))


def apply_to_invoice(deposit, invoice, *, actor_id=None):
    """Consume a deposit against an invoice via record_payment. Marks
    the deposit APPLIED and links it to the invoice. Uses the deposit's
    ORIGINAL payment method so the JE mirrors the customer's actual
    settlement history — no synthetic offset account."""
    from app.services.invoicing import record_payment
    if deposit.status != DEPOSIT_ACTIVE:
        raise DepositError("هذا العربون لم يعد متاحاً")
    if invoice.company_id != deposit.company_id:
        raise DepositError("العربون تابع لشركة أخرى")
    if invoice.customer_id != deposit.customer_id:
        raise DepositError("هذا العربون تابع لعميل مختلف")
    remaining = Decimal(str(invoice.total)) - Decimal(
        str(invoice.paid_amount or 0))
    if remaining <= 0:
        raise DepositError("الفاتورة مسدّدة بالكامل بالفعل")
    apply_amt = min(Decimal(str(deposit.amount)), remaining)

    record_payment(
        invoice=invoice, amount=float(apply_amt),
        payment_method_id=deposit.payment_method_id,
        payment_date=date.today(), created_by=actor_id,
        notify=False,
    )
    deposit.status = DEPOSIT_APPLIED
    deposit.applied_invoice_id = invoice.id
    db.session.commit()
    return deposit


def refund(deposit, *, actor_id=None):
    """Reverse the deposit JE (Dr customer_ar, Cr cash) and mark
    REFUNDED. Only valid on ACTIVE deposits."""
    if deposit.status != DEPOSIT_ACTIVE:
        raise DepositError("يمكن استرداد العربونات المتاحة فقط")
    ar = ensure_customer_account(deposit.customer)
    if not ar:
        raise LedgerError("تعذر إيجاد الحساب الفرعي")
    entry = post_journal(
        company_id=deposit.company_id,
        description=(f"استرداد عربون {deposit.doc_number} — "
                      f"{deposit.customer.name}"),
        lines=[
            {"account_id": ar.id,
             "debit": float(deposit.amount), "credit": 0,
             "memo": f"استرداد عربون {deposit.doc_number}"},
            {"account_id": deposit.payment_method.account_id,
             "debit": 0, "credit": float(deposit.amount),
             "memo": "استرداد نقدي"},
        ],
        entry_date=date.today(),
        reference=f"REV-{deposit.doc_number}",
        currency=(deposit.customer.company.base_currency
                   if deposit.customer.company else "EGP"),
        created_by=actor_id,
        source_type="customer_deposit_refund",
        source_id=deposit.id,
    )
    deposit.status = DEPOSIT_REFUNDED
    deposit.refund_journal_entry_id = entry.id
    # MARSOUD-DEPOSIT-AUDIT-01 (2026-08-06) — record who refunded and
    # when. Reception's audit trail was already on created_by_id from
    # record_deposit; refund had no matching column until this ticket.
    deposit.refunded_by_id = actor_id
    deposit.refunded_at = datetime.utcnow()
    db.session.commit()
    return deposit
