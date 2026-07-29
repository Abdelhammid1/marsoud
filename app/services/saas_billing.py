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

    # MARSOUD-SAAS-EMAIL-01 (2026-07-29) — email the customer
    # about their new subscription invoice. Wrapped in a try so
    # a mail-server hiccup doesn't roll back the invoice creation.
    _try_email(invoice)
    return invoice


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


# ─── Mark paid + renew ───────────────────────────────────────────
def mark_saas_invoice_paid(invoice, admin_user_id):
    """Called from /admin/saas mark-paid button. Records payment,
    renews the subscription (+1 cycle), redeems the coupon (if
    any), and creates the NEXT invoice with a future issue_date
    so the reminder engine picks it up naturally."""
    from app.services.invoicing import record_payment
    from app.services import coupons as _cp

    if invoice.status == InvoiceStatus.PAID:
        raise SaasBillingError("الفاتورة مدفوعة بالفعل")

    # Find the tenant company. The invoice sits in Manasty's books
    # so company_id = Manasty; the tenant is embedded in
    # internal_notes for cross-reference. We locate the tenant by
    # matching saas_customer_id back to the invoice's customer.
    tenant = Company.query.filter_by(
        saas_customer_id=invoice.customer_id).first()
    if not tenant:
        raise SaasBillingError(
            "لا نستطيع ربط الفاتورة بالشركة المستأجرة")

    # 1. Record the payment against the invoice.
    record_payment(
        invoice=invoice,
        amount=float(invoice.balance),
        payment_date=date.today(),
        method="cash",
        created_by=admin_user_id,
        notify=False,
    )

    # 2. Renew the tenant's subscription.
    days = 365 if tenant.subscription_frequency == FREQ_YEARLY else 30
    prev = tenant.subscription_expires_at or datetime.utcnow()
    base = max(prev, datetime.utcnow())
    tenant.subscription_expires_at = base + timedelta(days=days)
    if not tenant.subscription_started_at:
        tenant.subscription_started_at = datetime.utcnow()

    # 3. Redeem coupon if any. Per ticket: redeem() runs ONLY AFTER
    # payment succeeds (not before). We clear applied_coupon_id so
    # the discount doesn't re-apply on the NEXT invoice.
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
                # Non-fatal — the coupon may already be exhausted.
                # We still want the payment + renewal to stick.
                pass
        tenant.applied_coupon_id = None

    # 4. Create the NEXT invoice. Dated at the appropriate offset
    # so the existing reminder engine fires naturally.
    next_issue = next_billing_date(tenant)
    plan = db.session.get(Plan, tenant.intended_plan_id) \
        if tenant.intended_plan_id else None
    if plan:
        freq = tenant.subscription_frequency or FREQ_MONTHLY
        price = _resolve_price(tenant, plan, freq)
        cust = db.session.get(Customer, tenant.saas_customer_id)
        mid = cust.company_id
        freq_label = FREQ_LABELS_AR.get(freq, freq)
        plan_label = plan.name_ar or plan.name
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

    db.session.commit()

    # MARSOUD-SAAS-EMAIL-01 (2026-07-29) — email the customer
    # about their newly-created next-cycle invoice. Done AFTER
    # commit so a mail failure doesn't undo the renewal.
    if plan:
        _try_email(next_inv)
    return tenant


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
