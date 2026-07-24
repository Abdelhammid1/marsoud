"""MARSOUD-INSTALLMENT-PLAN-01 (Abdelhamid 2026-07-24).

Split one invoice into scheduled installments, collect them one at
a time, and refresh the invoice's roll-up status accordingly.
Reminders and overdue-flip hooks live here so the invoicing service
stays free of installment-specific branches.
"""
from datetime import date, datetime
from decimal import Decimal
from app import db
from app.models import (
    Invoice, InvoiceStatus, Payment,
    InvoiceInstallment, InstallmentReminderSent,
    INSTALLMENT_PENDING, INSTALLMENT_PAID, INSTALLMENT_OVERDUE,
    PaymentMethod,
)


class InstallmentError(Exception):
    """User-visible installment-plan error (sum mismatch, over-pay, etc.)."""


def create_installment_plan(invoice, rows, *, actor_id=None):
    """rows: list of {"amount": <str/Decimal>, "due_date": <ISO date str/date>}.

    Validates sum(rows.amount) == invoice.total to the cent. Refuses
    to overwrite an existing plan — the caller must clear first if
    they want to reschedule.
    """
    if not invoice or not invoice.id:
        raise InstallmentError("الفاتورة غير موجودة")
    if invoice.installments:
        raise InstallmentError(
            "الفاتورة عليها خطة أقساط بالفعل. احذفها أولاً.")
    if not rows or len(rows) < 2:
        raise InstallmentError(
            "خطة الأقساط يجب أن تحتوي على قسطين على الأقل")

    total_target = _q(invoice.total)
    total_rows = Decimal("0")
    parsed = []
    for i, r in enumerate(rows, start=1):
        amt = _q(r.get("amount"))
        if amt <= 0:
            raise InstallmentError(
                f"قسط رقم {i}: القيمة يجب أن تكون أكبر من صفر")
        due = r.get("due_date")
        if isinstance(due, str):
            due = datetime.strptime(due, "%Y-%m-%d").date()
        if not due:
            raise InstallmentError(f"قسط رقم {i}: تاريخ الاستحقاق مطلوب")
        parsed.append((amt, due))
        total_rows += amt
    if total_rows != total_target:
        raise InstallmentError(
            f"مجموع الأقساط ({total_rows}) لا يساوي قيمة الفاتورة "
            f"({total_target})")

    for i, (amt, due) in enumerate(parsed, start=1):
        db.session.add(InvoiceInstallment(
            invoice_id=invoice.id, sequence_no=i,
            amount=amt, due_date=due, status=INSTALLMENT_PENDING,
        ))
    db.session.commit()
    return invoice.installments


def pay_installment(installment, *, payment_method, actor_id=None,
                     payment_date=None):
    """Collect exactly this installment via record_payment(). Marks
    the installment PAID, links the resulting Payment, then re-rolls
    the invoice status."""
    from app.services.invoicing import record_payment
    if installment.status == INSTALLMENT_PAID:
        raise InstallmentError("هذا القسط مسدّد بالفعل")
    inv = installment.invoice
    if inv.status in (InvoiceStatus.CANCELLED, InvoiceStatus.VOIDED,
                      InvoiceStatus.REFUNDED):
        raise InstallmentError(
            "لا يمكن تحصيل قسط على فاتورة ملغاة أو مسترجعة")
    payment_date = payment_date or date.today()
    before_last_payment = Payment.query.filter_by(
        invoice_id=inv.id).order_by(Payment.id.desc()).first()

    record_payment(
        invoice=inv, amount=float(installment.amount),
        payment_method_id=(payment_method.id
                            if isinstance(payment_method, PaymentMethod)
                            else int(payment_method)),
        payment_date=payment_date, created_by=actor_id,
        notify=False,
    )

    # Find the payment record just created and link it.
    after_payment = Payment.query.filter_by(
        invoice_id=inv.id).order_by(Payment.id.desc()).first()
    if after_payment and after_payment != before_last_payment:
        installment.paid_payment_id = after_payment.id
    installment.status = INSTALLMENT_PAID
    installment.paid_at = datetime.utcnow()

    _rollup_invoice_status(inv)
    db.session.commit()
    return installment


def refresh_installment_overdue_flags(company_id=None):
    """Flip PENDING → OVERDUE for any installment whose due_date is
    past. Returns count flipped. Safe to run daily from cron."""
    q = InvoiceInstallment.query.filter(
        InvoiceInstallment.status == INSTALLMENT_PENDING,
        InvoiceInstallment.due_date < date.today(),
    )
    if company_id is not None:
        q = q.join(Invoice, InvoiceInstallment.invoice_id == Invoice.id)\
             .filter(Invoice.company_id == company_id)
    flipped = 0
    for i in q.all():
        i.status = INSTALLMENT_OVERDUE
        flipped += 1
    if flipped:
        db.session.commit()
    return flipped


def _rollup_invoice_status(invoice):
    """Compute invoice status from its installment set. Called after
    each installment collection."""
    if not invoice.installments:
        return
    statuses = {i.status for i in invoice.installments}
    if statuses == {INSTALLMENT_PAID}:
        invoice.status = InvoiceStatus.PAID
    elif INSTALLMENT_PAID in statuses:
        invoice.status = InvoiceStatus.PARTIALLY_PAID


def _q(v):
    """Coerce to a 2-decimal Decimal — the currency scale for
    invoice amounts."""
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))
