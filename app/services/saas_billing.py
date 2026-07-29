"""MARSOUD-SAAS-BILLING-01 (Batch 5 Ticket 7, 2026-07-29).

End-to-end SaaS billing loop. The flow:

  1. Owner picks a plan + frequency on /choose-plan (Auth.choose_plan
     calls create_first_invoice).
  2. We ensure Manasty has a Customer row mirroring this tenant
     (ensure_saas_customer) so the invoice + payment JE land on a
     real party ledger.
  3. A real Invoice is created in Manasty's books, dated today, due
     at the trial-end date. Coupon (if any) is applied as a
     PERCENT/FIXED invoice-level discount.
  4. Existing invoice-reminder engine (reminders.py) picks it up —
     no new cron code needed.
  5. Trial expires unpaid → subscription flips to read-only via
     the existing subscription_state() gate.
  6. Manasty admin clicks "تم الدفع" on /admin/saas:
     mark_saas_invoice_paid() records the payment, renews the
     subscription (+1 cycle), redeems the coupon (if any), and
     immediately creates the NEXT invoice so reminders fire
     naturally through the same cron engine.

Design choices:
- Payment gateway integration is out of scope (per plan). This
  service assumes manual payment confirmation.
- Plan price changes do NOT auto-apply. Company.price_lock, when
  set, overrides plan.price_monthly for THIS company's next
  invoice. Admin sets this per-customer.
- Monthly cycle = 30 days; yearly = 365 days. Aligned with the
  ticket's language and simpler than calendar-month math.
"""
from datetime import datetime, timedelta, date
from decimal import Decimal
from app import db
from app.models import (
    Company, Customer, Invoice, InvoiceItem, InvoiceStatus,
    DiscountType, Plan, Coupon,
)
from app.services.manasty import manasty_id
from app.services.subsidiary import ensure_customer_account
from app.services.numbering import next_number


FREQ_MONTHLY = "MONTHLY"
FREQ_YEARLY = "YEARLY"
ALL_FREQS = (FREQ_MONTHLY, FREQ_YEARLY)
FREQ_LABELS_AR = {
    FREQ_MONTHLY: "شهري",
    FREQ_YEARLY: "سنوي",
}


class SaasBillingError(Exception):
    """Anything the caller (route handler) should surface as a
    user-visible error."""


def _tenant_owner_email(company):
    """Return the owner's email for this tenant, or None. Looks up
    user_companies with role='owner' scoped to the tenant's id.
    Falls back to the first admin if no owner (defensive)."""
    from sqlalchemy import text
    row = db.session.execute(text(
        "SELECT u.email FROM users u "
        "JOIN user_companies uc ON uc.user_id = u.id "
        "WHERE uc.company_id = :c "
        "ORDER BY (CASE WHEN uc.role='owner' THEN 0 "
        "               WHEN uc.role='admin' THEN 1 ELSE 2 END), "
        "         u.id LIMIT 1"), {"c": company.id}).fetchone()
    return row[0] if row and row[0] else None


# ─── Customer mirror ─────────────────────────────────────────────
def ensure_saas_customer(company):
    """Get-or-create the Customer row in Manasty's books that
    represents this tenant. Idempotent. Copies the owner's email
    to Customer.email so the invoice notification has a real
    recipient (MARSOUD-SAAS-EMAIL-01, 2026-07-29)."""
    owner_email = _tenant_owner_email(company)
    if company.saas_customer_id:
        cust = db.session.get(Customer, company.saas_customer_id)
        if cust:
            # Keep the mirror's email in sync — the owner might
            # have changed their address between invoices.
            if owner_email and cust.email != owner_email:
                cust.email = owner_email
                db.session.flush()
            return cust
        # Stale FK — fall through and re-create.
    mid = manasty_id()
    cust = Customer(
        company_id=mid,
        name=company.name,
        email=owner_email,
        phone=None,
        is_active=True,
    )
    db.session.add(cust)
    db.session.flush()
    ensure_customer_account(cust)
    company.saas_customer_id = cust.id
    db.session.flush()
    return cust


# ─── Pricing ─────────────────────────────────────────────────────
def _resolve_price(company, plan, frequency):
    """price_lock takes precedence. Otherwise: plan.price_monthly
    × 1 (monthly) or plan.price_yearly (yearly)."""
    if company.price_lock is not None:
        return Decimal(str(company.price_lock))
    if frequency == FREQ_YEARLY:
        price = plan.price_yearly or (plan.price_monthly or 0) * 12
    else:
        price = plan.price_monthly or 0
    return Decimal(str(price))


def next_billing_date(company):
    """When to create the NEXT invoice after a payment.

    Monthly: 3 days before subscription_expires_at.
    Yearly:  30 days before subscription_expires_at.

    Returns a date. Falls back to today if the company has no
    expires_at (defensive)."""
    exp = company.subscription_expires_at
    if not exp:
        return date.today()
    offset = 30 if company.subscription_frequency == FREQ_YEARLY else 3
    return (exp - timedelta(days=offset)).date()


# ─── First-invoice creation ──────────────────────────────────────
def create_first_invoice(company):
    """Called at /choose-plan submit. Creates the first billing
    invoice in Manasty's books. Applies coupon (if company.
    applied_coupon_id is set). Returns the Invoice, or None if
    the company has no intended_plan yet.

    Idempotent: if an unpaid SaaS invoice already exists for this
    company, returns it unchanged (so a re-submit of /choose-plan
    doesn't spam duplicates)."""
    if not company.intended_plan_id:
        return None
    plan = db.session.get(Plan, company.intended_plan_id)
    if not plan:
        raise SaasBillingError("الباقة غير موجودة")
    freq = company.subscription_frequency or FREQ_MONTHLY

    cust = ensure_saas_customer(company)
    mid = cust.company_id

    # Idempotency: don't create a second invoice for a company that
    # already has an outstanding one.
    existing = Invoice.query.filter_by(
        company_id=mid, customer_id=cust.id,
    ).filter(
        Invoice.status.in_([
            InvoiceStatus.DRAFT, InvoiceStatus.SENT,
            InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.OVERDUE,
        ])
    ).first()
    if existing:
        return existing

    price = _resolve_price(company, plan, freq)
    due = (company.subscription_expires_at or
           datetime.utcnow() + timedelta(days=14)).date()

    plan_label = plan.name_ar or plan.name
    freq_label = FREQ_LABELS_AR.get(freq, freq)
    line_desc = f"اشتراك منصتي — باقة {plan_label} ({freq_label})"

    invoice = Invoice(
        company_id=mid,
        customer_id=cust.id,
        number=next_number(mid, "INVOICE"),
        issue_date=date.today(),
        due_date=due,
        currency=company.base_currency or "EGP",
        tax_rate=0,
        status=InvoiceStatus.SENT,
        source="SAAS_BILLING",
        notes=(f"فاتورة اشتراك تلقائية للشركة: {company.name}. "
               f"الفترة {freq_label}."),
        internal_notes=f"tenant_id={company.id};freq={freq}",
        send_reminders=True,
    )
    db.session.add(invoice)
    db.session.flush()

    item = InvoiceItem(
        invoice_id=invoice.id,
        company_id=mid,
        description=line_desc,
        quantity=1,
        unit_price=price,
    )
    db.session.add(item)

    # Coupon-as-invoice-discount. Applied on the invoice level so
    # the discount is visible on the PDF/table without altering the
    # unit price.
    if company.applied_coupon_id:
        coupon = db.session.get(Coupon, company.applied_coupon_id)
        if coupon and coupon.active:
            if coupon.discount_type == "PERCENT":
                invoice.invoice_discount_type = DiscountType.PERCENT
                invoice.invoice_discount_value = coupon.discount_value
            else:
                invoice.invoice_discount_type = DiscountType.FIXED
                invoice.invoice_discount_value = coupon.discount_value

    invoice.recalc()
    db.session.flush()

    # MARSOUD-SAAS-INVOICE-LEDGER-01 (Batch 6 Ticket 6, 2026-07-29)
    # — post the journal entry BEFORE emailing, inside the same
    # transaction. If ledger posting fails, the whole invoice
    # rolls back so we never leave an invoice orphan without a
    # matching journal entry (which would break AR aging + the
    # customer sub-account balance). Idempotent via
    # _has_journal_entry() so callers can safely retry.
    _post_to_ledger_idempotent(invoice)

    # MARSOUD-SAAS-EMAIL-01 (2026-07-29) — email the customer
    # about their new subscription invoice. Wrapped in a try so
    # a mail-server hiccup doesn't roll back the invoice creation.
    _try_email(invoice)
    return invoice


def _has_journal_entry(invoice):
    """MARSOUD-SAAS-INVOICE-LEDGER-01 idempotency check. Returns
    True if this invoice already has at least one journal entry
    posted for it. Guards against double-posting on retries + on
    the backfill script rerun."""
    from sqlalchemy import text
    row = db.session.execute(text(
        "SELECT id FROM journal_entries "
        "WHERE source_type = 'invoice' AND source_id = :i "
        "LIMIT 1"), {"i": invoice.id}).fetchone()
    return row is not None


def _post_to_ledger_idempotent(invoice, created_by=None):
    """Post the invoice to the ledger unless it already has an
    entry. Any LedgerError propagates so the caller's outer
    transaction rolls back — orphan invoices are worse than a
    failed signup."""
    from app.services.invoicing import post_invoice_to_ledger
    if _has_journal_entry(invoice):
        return None
    return post_invoice_to_ledger(invoice, created_by=created_by)


def _try_email(invoice):
    """Send the invoice email best-effort; swallow errors so the
    caller's DB transaction isn't affected. Used by both
    create_first_invoice and mark_saas_invoice_paid for the
    next-cycle invoice."""
    from app.services.invoicing import send_invoice_notification
    try:
        send_invoice_notification(invoice)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger("ledgeros.saas_billing").exception(
            "SaaS invoice email failed for invoice #%s", invoice.id)


# ─── Post-payment side-effects (renew + next invoice + coupon) ──
def _saas_post_payment(invoice, admin_user_id):
    """Runs AFTER a SaaS invoice has been paid. Called by BOTH
    mark_saas_invoice_paid() and — via a post-commit hook — from
    invoicing.record_payment() so paying a SaaS invoice from the
    regular invoices screen produces the same effect as clicking
    'تم الدفع' on /admin/saas.

    Returns (tenant, next_invoice_or_None). Idempotent: if the
    tenant's subscription was already renewed by an earlier call
    on this same invoice, we skip renewal + skip next-invoice
    creation (both flagged by the invoice's `internal_notes`
    picking up a ';post_payment=1' marker)."""
    from app.services import coupons as _cp

    tenant = Company.query.filter_by(
        saas_customer_id=invoice.customer_id).first()
    if not tenant:
        raise SaasBillingError(
            "لا نستطيع ربط الفاتورة بالشركة المستأجرة")

    # Idempotency: refuse to double-run on the same invoice.
    marker = ";post_payment=1"
    if invoice.internal_notes and marker in invoice.internal_notes:
        return tenant, None
    invoice.internal_notes = (invoice.internal_notes or "") + marker

    # 1. Renew the tenant's subscription.
    days = 365 if tenant.subscription_frequency == FREQ_YEARLY else 30
    prev = tenant.subscription_expires_at or datetime.utcnow()
    base = max(prev, datetime.utcnow())
    tenant.subscription_expires_at = base + timedelta(days=days)
    if not tenant.subscription_started_at:
        tenant.subscription_started_at = datetime.utcnow()

    # 2. Redeem coupon if any. Per ticket: redeem() runs ONLY AFTER
    # payment succeeds (not before). Clear applied_coupon_id so the
    # discount doesn't re-apply on the NEXT invoice.
    if tenant.applied_coupon_id:
        coupon = db.session.get(Coupon, tenant.applied_coupon_id)
        if coupon:
            from app.models import User
            admin = db.session.get(User, admin_user_id)
            try:
                _cp.redeem(coupon, tenant, admin,
                           amount_saved=Decimal(str(
                               invoice.invoice_discount_amount or 0)))
            except _cp.CouponError:
                # Non-fatal — coupon may already be exhausted.
                pass
        tenant.applied_coupon_id = None

    # 3. Create the NEXT invoice. Dated at the appropriate offset
    # so the existing reminder engine fires naturally.
    next_inv = None
    plan = db.session.get(Plan, tenant.intended_plan_id) \
        if tenant.intended_plan_id else None
    if plan:
        freq = tenant.subscription_frequency or FREQ_MONTHLY
        price = _resolve_price(tenant, plan, freq)
        cust = db.session.get(Customer, tenant.saas_customer_id)
        mid = cust.company_id
        freq_label = FREQ_LABELS_AR.get(freq, freq)
        plan_label = plan.name_ar or plan.name
        next_issue = next_billing_date(tenant)
        next_inv = Invoice(
            company_id=mid,
            customer_id=cust.id,
            number=next_number(mid, "INVOICE"),
            issue_date=next_issue,
            due_date=tenant.subscription_expires_at.date(),
            currency=tenant.base_currency or "EGP",
            tax_rate=0,
            status=InvoiceStatus.SENT,
            source="SAAS_BILLING",
            notes=(f"فاتورة اشتراك تلقائية للشركة: {tenant.name}. "
                   f"الفترة {freq_label}."),
            internal_notes=f"tenant_id={tenant.id};freq={freq}",
            send_reminders=True,
        )
        db.session.add(next_inv)
        db.session.flush()
        db.session.add(InvoiceItem(
            invoice_id=next_inv.id,
            company_id=mid,
            description=f"اشتراك منصتي — باقة {plan_label} ({freq_label})",
            quantity=1,
            unit_price=price,
        ))
        next_inv.recalc()
        db.session.flush()
        # MARSOUD-SAAS-INVOICE-LEDGER-01 (Batch 6 Ticket 6,
        # 2026-07-29) — post the JE for the next-cycle invoice
        # inside the same transaction. Same idempotency guard as
        # create_first_invoice.
        _post_to_ledger_idempotent(next_inv,
                                     created_by=admin_user_id)
    return tenant, next_inv


# ─── Mark paid + renew ───────────────────────────────────────────
def mark_saas_invoice_paid(invoice, admin_user_id):
    """Called from /admin/saas mark-paid button. Records payment
    then runs the shared post-payment routine."""
    from app.services.invoicing import record_payment

    if invoice.status == InvoiceStatus.PAID:
        raise SaasBillingError("الفاتورة مدفوعة بالفعل")

    # 1. Record the payment against the invoice. record_payment
    # will trigger _saas_post_payment via its post-commit hook, so
    # we don't call the helper explicitly here.
    record_payment(
        invoice=invoice,
        amount=float(invoice.balance),
        payment_date=date.today(),
        method="cash",
        created_by=admin_user_id,
        notify=False,
    )
    # record_payment already ran _saas_post_payment via the hook +
    # committed. Just look up the tenant to return it, and email
    # the next invoice if one was created.
    tenant = Company.query.filter_by(
        saas_customer_id=invoice.customer_id).first()
    next_inv = _find_latest_next_invoice(tenant) if tenant else None
    if next_inv:
        _try_email(next_inv)
    return tenant


def _find_latest_next_invoice(tenant):
    """Return the most-recently-created outstanding SaaS invoice
    for this tenant, or None. Used to know which invoice to email
    after mark_saas_invoice_paid() runs."""
    if not tenant or not tenant.saas_customer_id:
        return None
    return (Invoice.query
              .filter(Invoice.customer_id == tenant.saas_customer_id,
                       Invoice.source == "SAAS_BILLING",
                       Invoice.status.in_([
                           InvoiceStatus.DRAFT, InvoiceStatus.SENT]))
              .order_by(Invoice.id.desc())
              .first())


def outstanding_saas_invoices():
    """List all unpaid Manasty-side SaaS invoices, ordered by due
    date. Powers the /admin/saas page."""
    mid = manasty_id()
    return (Invoice.query
              .filter(Invoice.company_id == mid,
                       Invoice.source == "SAAS_BILLING",
                       Invoice.status.in_([
                           InvoiceStatus.DRAFT, InvoiceStatus.SENT,
                           InvoiceStatus.PARTIALLY_PAID,
                           InvoiceStatus.OVERDUE,
                       ]))
              .order_by(Invoice.due_date.asc())
              .all())
