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

    # MARSOUD-INVOICE-INSTALLMENTS-DISPLAY-01 (2026-09-08) — validate
    # against the invoice's REMAINING balance, not its full total.
    # For every existing caller (plan created on an untouched invoice)
    # balance == total, so behavior is byte-identical. For the new
    # create-form caller — where a down-payment has already been
    # recorded via record_payment before this runs — the plan
    # legitimately covers only (total - down_payment), and validating
    # against the full total would refuse a correct plan.
    total_target = _q(float(invoice.total or 0)
                      - float(invoice.paid_amount or 0))
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
            f"مجموع الأقساط ({total_rows}) لا يساوي المبلغ المتبقّي على الفاتورة "
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

    # MARSOUD-INVOICE-INSTALLMENT-EMAILS-01 (2026-09-08) — thank-you
    # after each installment collection. Uses the same Tabby-style
    # timeline template so the customer sees the paid row struck
    # through + the next pending row highlighted as "coming up".
    # Don't pass `installment=` — the email function auto-picks the
    # next PENDING row (i.e. the one the customer owes next), not
    # the one we just cleared. Non-blocking: a mail failure must
    # not roll back the payment.
    try:
        from app.services.email import send_installment_email
        send_installment_email(inv, kind="payment_received",
                                payment=after_payment)
    except Exception:
        import logging
        logging.getLogger("ledgeros.installments").exception(
            "Failed to send installment payment_received email "
            "for invoice %s installment %s",
            inv.number, installment.sequence_no)

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


# ─── MARSOUD-INVOICE-CUSTOM-INSTALLMENTS-01 (2026-10-10) ──────────
# The equal-split flow computes rows from `installment_count` +
# `installment_start_date`; the new custom flow reads parallel
# `custom_due_date[]` + `custom_amount[]` arrays off the form.  The
# helpers below cover: parsing the arrays, ticket-level validation
# (max 24, date floor, no duplicates, auto-sort, Decimal compare),
# and the "save as quote" ↔ "send" bridge via `invoices.pending_plan_json`.

MAX_INSTALLMENTS = 24


def _parse_custom_rows(form):
    """Pull parallel `custom_due_date[]` + `custom_amount[]` arrays
    off the form, dropping empty rows, returning
    `[{amount: Decimal, due_date: date}, ...]`.  Keeps the raw text
    order — downstream `_validate_custom_rows` sorts and dedupes."""
    from datetime import datetime as _dt
    dates = form.getlist("custom_due_date[]") if hasattr(
        form, "getlist") else form.get("custom_due_date[]") or []
    amounts = form.getlist("custom_amount[]") if hasattr(
        form, "getlist") else form.get("custom_amount[]") or []
    rows = []
    for d_raw, a_raw in zip(dates, amounts):
        d_raw = (d_raw or "").strip()
        a_raw = (a_raw or "").strip()
        if not d_raw and not a_raw:
            continue  # fully-empty row — operator-added placeholder
        try:
            due = _dt.strptime(d_raw, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise InstallmentError(
                f"تاريخ قسط غير صالح: {d_raw!r}")
        try:
            amt = Decimal(a_raw)
        except (ArithmeticError, TypeError, ValueError):
            raise InstallmentError(
                f"مبلغ قسط غير صالح: {a_raw!r}")
        rows.append({"amount": _q(amt), "due_date": due})
    return rows


def _validate_custom_rows(rows, invoice_total, down_payment, issue_date):
    """Enforce the ticket-level rules on top of the service's own
    sum-vs-remaining check:
      * at least one row
      * at most MAX_INSTALLMENTS rows (24)
      * every amount > 0
      * every due_date >= issue_date
      * no duplicate due_dates
      * Σ amounts (Decimal) == invoice_total − down_payment to the cent
      * returns a NEW list sorted by due_date so sequence_no reflects
        chronological order (oldest-first distribution).
    Raises InstallmentError with an Arabic message naming the exact
    diff so the operator can self-correct.
    """
    if not rows:
        raise InstallmentError("أدخل قسطًا واحدًا على الأقل")
    if len(rows) > MAX_INSTALLMENTS:
        raise InstallmentError(
            f"عدد الأقساط ({len(rows)}) أكبر من الحد الأقصى "
            f"({MAX_INSTALLMENTS})")
    seen_dates = set()
    for i, r in enumerate(rows, start=1):
        if r["amount"] <= 0:
            raise InstallmentError(
                f"قسط رقم {i}: القيمة يجب أن تكون أكبر من صفر")
        if issue_date is not None and r["due_date"] < issue_date:
            raise InstallmentError(
                f"قسط رقم {i}: تاريخ الاستحقاق قبل تاريخ الفاتورة "
                f"({issue_date})")
        if r["due_date"] in seen_dates:
            raise InstallmentError(
                f"تاريخ مكرّر: {r['due_date']} — كل قسط يجب أن يكون "
                f"بتاريخ مختلف")
        seen_dates.add(r["due_date"])
    expected = _q(invoice_total) - _q(down_payment or 0)
    got = sum((r["amount"] for r in rows), Decimal("0"))
    if got != expected:
        diff = got - expected
        raise InstallmentError(
            f"مجموع الأقساط ({got}) لا يساوي الباقي بعد الدفعة المقدّمة "
            f"({expected}) — الفرق {diff}")
    return sorted(rows, key=lambda r: r["due_date"])


def serialize_pending_plan(mode, rows, down_payment_amount,
                             down_payment_method_id):
    """Return a JSON string suitable for `invoices.pending_plan_json`.
    Caller validated `rows` already."""
    import json as _json
    return _json.dumps({
        "mode": mode,
        "down_payment_amount": str(_q(down_payment_amount or 0)),
        "down_payment_method_id": (int(down_payment_method_id)
                                     if down_payment_method_id else None),
        "rows": [
            {"due_date": r["due_date"].isoformat(),
             "amount": str(_q(r["amount"]))}
            for r in rows
        ],
    }, ensure_ascii=False)


def apply_pending_plan(invoice, *, actor_id=None):
    """Read `invoice.pending_plan_json`, record the stored down-payment
    via `record_payment`, hand the rows to `create_installment_plan`,
    then clear the column.  No-op (returns None) when the invoice has
    no stored plan — existing-invoice sends behave exactly as before.

    Used by the /send route: the plan was already validated at
    quote-save time, so Send does NOT re-validate — the ticket's
    'يتحول لفاتورة عند الإرسال بدون ما يتفقد' rule.
    """
    import json as _json
    from datetime import datetime as _dt
    if not invoice.pending_plan_json:
        return None
    try:
        payload = _json.loads(invoice.pending_plan_json)
    except (ValueError, TypeError):
        raise InstallmentError("خطة الأقساط المخزّنة تالفة")
    dp_amount = _q(payload.get("down_payment_amount") or 0)
    dp_method_id = payload.get("down_payment_method_id")
    if dp_amount > 0:
        if not dp_method_id:
            raise InstallmentError(
                "طريقة الدفع للمقدّم مفقودة في الخطة المخزّنة")
        from app.services.invoicing import record_payment
        record_payment(invoice, float(dp_amount),
                        payment_method_id=int(dp_method_id),
                        created_by=actor_id, notify=False)
    rows = []
    for r in payload.get("rows") or []:
        rows.append({
            "amount": _q(r.get("amount")),
            "due_date": _dt.strptime(r["due_date"], "%Y-%m-%d").date(),
        })
    create_installment_plan(invoice, rows, actor_id=actor_id)
    invoice.pending_plan_json = None
    db.session.commit()
    return invoice.installments
