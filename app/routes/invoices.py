from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, redirect, url_for, flash, request, g, send_file
from flask_login import login_required, current_user
from app import db
from app.models import Invoice, InvoiceItem, InvoiceStatus, Customer, Payment, Product, PaymentMethod, DiscountType
from app.models.refund import RefundType
from app.services.invoicing import (
    post_invoice_to_ledger, record_payment, issue_refund,
    update_overdue_statuses, send_invoice_notification,
)
from app.services.ledger import LedgerError
from app.services.numbering import next_number
from app.services.permissions import require_permission

bp = Blueprint("invoices", __name__)


def _next_number(company_id):
    return next_number(company_id, "INVOICE")


def _safe_float(raw, default=0):
    """MARSOUD-FIX-INVOICE-FLOAT (Abdelhamid 2026-07-25).

    Backport of vendor_bills._safe_float — same "empty/whitespace/
    comma-formatted string crashes float()" bug applies here too.
    Any raw form value passed through here becomes a float safely,
    or falls back to `default` on unparseable input. Never raises.
    """
    if raw is None:
        return default
    s = str(raw).strip()
    if not s:
        return default
    s = s.replace(",", "")
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


@bp.route("/")
@login_required
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    update_overdue_statuses(g.active_company.id)

    from app.models import Customer
    q = Invoice.query.filter_by(company_id=g.active_company.id)

    status = request.args.get("status")
    if status:
        try:
            q = q.filter_by(status=InvoiceStatus[status])
        except KeyError:
            pass

    # MARSOUD-INVOICES-RESTORE-01 (Batch 8 Ticket 3, 2026-07-30) —
    # deleted-invoice visibility filter. `active` (default) hides
    # voided invoices from the list; `deleted` shows ONLY voided
    # ones (so users can find + restore them); `all` shows both.
    # The Restore button in the template appears only when
    # invoice.voided_at is not NULL.
    # MARSOUD-VOIDED-VISIBLE (Batch 9 Ticket 2, 2026-08-01) —
    # user clarified voided invoices must stay in the list marked
    # as deleted (for reference), NOT hidden by default. Financial
    # totals (_EXCLUDED below) already exclude VOIDED so KPIs
    # don't inflate. Flipped default from `active` to `all`.
    deleted_filter = request.args.get("deleted_filter", "all")
    if deleted_filter == "active":
        q = q.filter(Invoice.voided_at.is_(None))
    elif deleted_filter == "deleted":
        q = q.filter(Invoice.voided_at.isnot(None))
    else:
        deleted_filter = "all"

    search = (request.args.get("search") or "").strip()
    if search:
        like = f"%{search}%"
        q = q.outerjoin(Customer, Invoice.customer_id == Customer.id).filter(
            db.or_(Invoice.number.ilike(like), Customer.name.ilike(like))
        )

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    if start_date:
        try:
            q = q.filter(Invoice.issue_date >= datetime.strptime(start_date, "%Y-%m-%d").date())
        except ValueError:
            pass
    if end_date:
        try:
            q = q.filter(Invoice.issue_date <= datetime.strptime(end_date, "%Y-%m-%d").date())
        except ValueError:
            pass

    invoices = q.order_by(Invoice.issue_date.desc(), Invoice.id.desc()).all()

    # MARSOUD-FIX-INVOICE-TOTALS-CANCELLED (Abdelhamid 2026-07-24) —
    # CANCELLED / VOIDED / REFUNDED invoices used to inflate
    # total_invoiced and total_collected. Now every top-of-page KPI
    # uses the same exclusion set as _ar_balance_as_of() in
    # reports.py, so the numbers agree across the app.
    _EXCLUDED = (
        InvoiceStatus.CANCELLED,
        InvoiceStatus.VOIDED,
        InvoiceStatus.REFUNDED,
    )
    countable = [i for i in invoices if i.status not in _EXCLUDED]
    total_invoiced = sum(float(i.total or 0) for i in countable)
    total_collected = sum(float(i.paid_amount or 0) for i in countable)
    total_outstanding = sum(i.balance for i in countable)

    totals = {
        "invoiced": total_invoiced,
        "collected": total_collected,
        "outstanding": total_outstanding,
        # Row count stays as the full filtered list — the KPI cards
        # under "المُفوتَر / المُحصَّل / المتبقي" are billable only,
        # but users still expect to see every row of their filter.
        "count": len(invoices),
    }

    return render_template(
        "invoices/index.html",
        invoices=invoices, statuses=InvoiceStatus, totals=totals,
        deleted_filter=deleted_filter,
    )


def _populate_invoice_from_form(invoice, form):
    """Apply form data to an Invoice (used by both create and edit)."""
    invoice.customer_id = int(form.get("customer_id"))
    invoice.issue_date = datetime.strptime(form.get("issue_date", date.today().isoformat()), "%Y-%m-%d").date()
    invoice.due_date = datetime.strptime(form.get("due_date", (date.today() + timedelta(days=30)).isoformat()), "%Y-%m-%d").date()
    # MARSOUD-INVOICE-TAX-ZERO (Batch 9 Ticket 1, 2026-08-01) —
    # explicit None-check preserves vat_rate=0 (the new-company
    # default from Batch 8 Ticket 4b). `X or 15` was returning 15
    # for 0% companies.
    _co_vat = g.active_company.vat_rate
    invoice.tax_rate = _safe_float(
        form.get("tax_rate"),
        default=float(_co_vat if _co_vat is not None else 0))
    invoice.notes = form.get("notes", "")
    invoice.internal_notes = form.get("internal_notes", "")
    invoice.send_reminders = form.get("send_reminders") == "1"

    try:
        invoice.invoice_discount_type = DiscountType[(form.get("invoice_discount_type") or "NONE")]
    except KeyError:
        invoice.invoice_discount_type = DiscountType.NONE
    invoice.invoice_discount_value = _safe_float(
        form.get("invoice_discount_value"), 0)

    # Replace items
    for old in list(invoice.items):
        db.session.delete(old)
    db.session.flush()

    product_ids = form.getlist("item_product_id[]")
    descriptions = form.getlist("item_description[]")
    quantities = form.getlist("item_quantity[]")
    unit_prices = form.getlist("item_unit_price[]")
    disc_types = form.getlist("item_discount_type[]")
    disc_values = form.getlist("item_discount_value[]")
    # MARSOUD-UNIT-CONVERSION-01 — unit picker on each row.
    unit_ids = form.getlist("item_unit_id[]")

    for i, desc in enumerate(descriptions):
        if not (desc or "").strip():
            continue
        pid = product_ids[i] if i < len(product_ids) and product_ids[i] else None
        try:
            item_dt = DiscountType[(disc_types[i] if i < len(disc_types) else "NONE") or "NONE"]
        except KeyError:
            item_dt = DiscountType.NONE
        uid_raw = unit_ids[i] if i < len(unit_ids) else None
        try:
            uid = int(uid_raw) if uid_raw else None
        except (TypeError, ValueError):
            uid = None
        item = InvoiceItem(
            invoice_id=invoice.id,
            company_id=invoice.company_id,
            product_id=int(pid) if pid else None,
            description=desc.strip(),
            quantity=_safe_float(
                quantities[i] if i < len(quantities) else None, 1),
            unit_price=_safe_float(
                unit_prices[i] if i < len(unit_prices) else None, 0),
            discount_type=item_dt,
            discount_value=_safe_float(
                disc_values[i] if i < len(disc_values) else None, 0),
            unit_id=uid,
        )
        db.session.add(item)
    db.session.flush()
    invoice.items = InvoiceItem.query.filter_by(invoice_id=invoice.id).all()
    invoice.recalc()


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("invoices.create")
def new():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    customers = Customer.query.filter_by(company_id=g.active_company.id, is_active=True).order_by(Customer.name).all()
    if request.method == "POST":
        try:
            invoice = Invoice(
                company_id=g.active_company.id,
                number=_next_number(g.active_company.id),
                customer_id=int(request.form.get("customer_id")),
                issue_date=date.today(),
                due_date=date.today() + timedelta(days=30),
                currency=g.active_company.base_currency,
                # MARSOUD-INVOICE-TAX-ZERO (Batch 9 Ticket 1) —
                # placeholder; the real value gets set inside
                # _populate_invoice_from_form a few lines down,
                # which now respects vat_rate=0.
                tax_rate=(g.active_company.vat_rate
                          if g.active_company.vat_rate is not None
                          else 0),
                status=InvoiceStatus.DRAFT,
                # MARSOUD-INVOICE-CREATOR — record who authored it.
                created_by_id=current_user.id,
            )
            db.session.add(invoice)
            db.session.flush()
            _populate_invoice_from_form(invoice, request.form)

            should_send = request.form.get("send") == "1"
            email_customer = request.form.get("email_customer") == "1"
            if should_send:
                invoice.status = InvoiceStatus.SENT
                post_invoice_to_ledger(invoice, created_by=current_user.id)
            db.session.commit()
            try:
                from app.services.superadmin import log_platform_action
                log_platform_action("invoice_created",
                                    target_company_id=invoice.company_id,
                                    actor_id=current_user.id,
                                    details=f"#{invoice.number}")
            except Exception:
                pass
            if should_send and email_customer:
                send_invoice_notification(invoice)
            flash(f"تم إنشاء الفاتورة {invoice.number}", "success")
            return redirect(url_for("invoices.view", invoice_id=invoice.id))
        except LedgerError as e:
            db.session.rollback()
            flash(str(e), "error")
        except Exception as e:
            db.session.rollback()
            flash(f"خطأ: {e}", "error")

    return render_template("invoices/form.html", customers=customers, invoice=None)


@bp.route("/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("invoices.create")
def edit(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    if invoice.status != InvoiceStatus.DRAFT:
        flash("لا يمكن تعديل فاتورة بعد إرسالها", "warning")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))

    customers = Customer.query.filter_by(company_id=g.active_company.id, is_active=True).order_by(Customer.name).all()
    if request.method == "POST":
        try:
            _populate_invoice_from_form(invoice, request.form)
            db.session.commit()
            flash("تم حفظ التعديلات", "success")
            return redirect(url_for("invoices.view", invoice_id=invoice.id))
        except Exception as e:
            db.session.rollback()
            flash(f"خطأ: {e}", "error")

    return render_template("invoices/form.html", customers=customers, invoice=invoice)


# ─── MARSOUD-CURRENCY-TAX-DEFAULTS (Batch 8 Ticket 4c, 2026-07-30) ──
_ALLOWED_INVOICE_CURRENCIES = ("EGP", "SAR", "AED", "USD", "EUR")


@bp.route("/<int:invoice_id>/change-currency", methods=["POST"])
@login_required
@require_permission("invoices.create")
def change_currency(invoice_id):
    """Relabel the currency on a posted invoice + sync its JE
    so the ledger stays consistent. Amounts are NOT converted —
    this is purely a label fix for invoices that were created
    with the wrong currency. Refused on VOIDED (already
    deleted). Any other status is allowed."""
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    if invoice.status == InvoiceStatus.VOIDED:
        flash("لا يمكن تعديل عملة فاتورة محذوفة", "error")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    new_currency = (request.form.get("currency") or "").strip().upper()
    if new_currency not in _ALLOWED_INVOICE_CURRENCIES:
        flash("عملة غير صالحة", "error")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    if new_currency == invoice.currency:
        flash("العملة الحالية زي المطلوبة — لا حاجة للتعديل", "warning")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    old = invoice.currency
    invoice.currency = new_currency
    # Keep the linked JE in sync — otherwise trial balance /
    # multi-currency reports get inconsistent.
    from sqlalchemy import text
    db.session.execute(text(
        "UPDATE journal_entries SET currency = :c "
        "WHERE source_type = 'invoice' AND source_id = :i"),
        {"c": new_currency, "i": invoice.id})
    db.session.commit()
    # Activity log for the audit trail.
    try:
        from app.services.activity import log_action
        log_action(
            action_type="UPDATE", entity_type="invoice",
            entity_id=invoice.id,
            entity_label=f"عملة الفاتورة {invoice.number}: "
                          f"{old} → {new_currency}",
            company_id=invoice.company_id,
            extra_data={"old_currency": old,
                        "new_currency": new_currency},
        )
    except Exception:
        pass
    flash(f"تم تغيير العملة من {old} إلى {new_currency}", "success")
    return redirect(url_for("invoices.view", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/preview")
@login_required
def preview_pdf(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    from app.services.export import export_invoice_pdf
    buf = export_invoice_pdf(invoice)
    return send_file(
        buf, mimetype="application/pdf",
        download_name=f"invoice-{invoice.number}.pdf",
        as_attachment=False,
    )


@bp.route("/<int:invoice_id>")
@login_required
def view(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    # MARSOUD-CUSTOMER-DEPOSIT-01 UI — pull active deposits for the
    # invoice's customer so the sidebar can offer a one-click apply.
    active_deposits = []
    if invoice.customer_id:
        from app.services.deposits import active_deposits_for_customer
        active_deposits = active_deposits_for_customer(invoice.customer_id)
    # MARSOUD-INSTALLMENT-PLAN-01 UI — payment methods for the per-
    # installment collect button.
    from app.models import PaymentMethod
    payment_methods = PaymentMethod.query.filter_by(
        company_id=invoice.company_id, is_active=True,
    ).order_by(PaymentMethod.is_default.desc(),
                PaymentMethod.name.asc()).all()
    return render_template("invoices/view.html", invoice=invoice,
                             refund_types=RefundType,
                             active_deposits=active_deposits,
                             active_deposits_pm_list=payment_methods)


@bp.route("/<int:invoice_id>/send", methods=["POST"])
@login_required
@require_permission("invoices.send")
def send(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    if invoice.status != InvoiceStatus.DRAFT:
        # MARSOUD-TKT-INVOICE-DRAFT-LABEL-QUOTE — tenant-visible copy
        # matches the button + badge rename. Enum unchanged.
        flash("الفاتورة ليست عرض سعر", "warning")
    else:
        try:
            invoice.status = InvoiceStatus.SENT
            post_invoice_to_ledger(invoice, created_by=current_user.id)
            if request.form.get("email_customer", "1") == "1":
                send_invoice_notification(invoice)
            flash("تم إرسال الفاتورة وتسجيل القيد", "success")
        except LedgerError as e:
            flash(str(e), "error")
    return redirect(url_for("invoices.view", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/resend", methods=["POST"])
@login_required
@require_permission("invoices.send")
def resend(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    if invoice.status == InvoiceStatus.DRAFT:
        flash("لا يمكن إعادة إرسال عرض سعر — أرسله أولاً", "warning")
    else:
        ok = send_invoice_notification(invoice)
        flash("تم إعادة الإرسال" if ok else "تعذّر الإرسال — راجع السجلات", "success" if ok else "error")
    return redirect(url_for("invoices.view", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/send-overdue-reminder", methods=["POST"])
@login_required
@require_permission("invoices.send")
def send_overdue_reminder_route(invoice_id):
    """MARSOUD-OVERDUE-REMINDER — one-click "إرسال تذكير" button for
    overdue invoices. The email template already reads every field the
    ticket listed (customer, invoice #, dates, amounts, days late,
    company data via Company.document_context) — this just wraps the
    existing send_overdue_reminder() service in an HTTP endpoint and
    logs the send in InvoiceReminderSent so the user can see a
    timestamp on the invoice view."""
    from datetime import date
    from app.models import InvoiceReminderSent
    from app.services.email import send_overdue_reminder
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    if invoice.status not in (InvoiceStatus.SENT, InvoiceStatus.OVERDUE):
        flash("التذكير متاح فقط للفواتير المرسلة/المتأخرة", "warning")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    if not invoice.customer or not (invoice.customer.email or "").strip():
        flash("العميل مالوش إيميل — عدّل بياناته الأول.", "error")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    days_late = (date.today() - invoice.due_date).days
    if days_late <= 0:
        flash("الفاتورة لسه مش متأخرة — استخدم زرار إعادة الإرسال العادي.",
               "warning")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    ok = send_overdue_reminder(invoice, f"overdue_{days_late}")
    if ok:
        # Record the send so the invoice view can render a
        # "آخر تذكير بتاريخ ..." line. Use a manual threshold
        # (999) for manual sends so it doesn't collide with the
        # cron-driven thresholds (7, 15, 30).
        try:
            db.session.add(InvoiceReminderSent(
                invoice_id=invoice.id,
                company_id=invoice.company_id,
                threshold_kind="overdue",
                threshold_days=days_late,
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
        flash(
            f"تم إرسال تذكير للعميل {invoice.customer.name} "
            f"(متأخرة {days_late} يوم)",
            "success",
        )
    else:
        flash("تعذّر الإرسال — راجع سجلات SMTP", "error")
    return redirect(url_for("invoices.view", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/pay", methods=["POST"])
@login_required
@require_permission("invoices.create")
def pay(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    try:
        amount = _safe_float(request.form.get("amount"), 0)
        pmid = request.form.get("payment_method_id") or None
        notify = request.form.get("notify_customer", "1") == "1"
        record_payment(
            invoice, amount,
            payment_method_id=int(pmid) if pmid else None,
            created_by=current_user.id, notify=notify,
        )
        try:
            from app.services.superadmin import log_platform_action
            log_platform_action("invoice_paid",
                                target_company_id=invoice.company_id,
                                actor_id=current_user.id,
                                details=f"#{invoice.number} amount={amount:.2f}")
        except Exception:
            pass
        flash(f"تم تسجيل دفعة {amount:.2f}", "success")
    except LedgerError as e:
        flash(str(e), "error")
    return redirect(url_for("invoices.view", invoice_id=invoice_id))


# MARSOUD-INSTALLMENT-PLAN-01 UI (Abdelhamid 2026-07-24) — three
# endpoints: create a plan, pay one installment, drop the plan.
@bp.route("/<int:invoice_id>/installments/plan", methods=["POST"])
@login_required
@require_permission("invoices.create")
def create_installments(invoice_id):
    from datetime import datetime as _dt
    from app.services.installments import (
        create_installment_plan, InstallmentError,
    )
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        abort(404)
    # Two shapes: "count" (auto-distribute evenly starting from
    # start_date, monthly gap) OR explicit "amounts[]" + "due_dates[]".
    amounts = request.form.getlist("amount[]")
    dates = request.form.getlist("due_date[]")
    if amounts and dates and len(amounts) == len(dates):
        rows = [{"amount": a, "due_date": d}
                 for a, d in zip(amounts, dates) if a and d]
    else:
        count = int(request.form.get("count") or 0)
        if count < 2:
            flash("عدد الأقساط يجب أن يكون 2 على الأقل", "error")
            return redirect(url_for("invoices.view",
                                      invoice_id=invoice.id))
        start_raw = (request.form.get("start_date") or "").strip()
        try:
            start = (_dt.strptime(start_raw, "%Y-%m-%d").date()
                     if start_raw else invoice.due_date or date.today())
        except ValueError:
            start = invoice.due_date or date.today()
        from decimal import Decimal, ROUND_HALF_UP
        total = Decimal(str(invoice.total or 0))
        per = (total / Decimal(count)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        rows = []
        current = start
        remaining = total
        for i in range(count):
            amt = per if i < count - 1 else remaining
            rows.append({
                "amount": str(amt),
                "due_date": current.isoformat(),
            })
            remaining -= amt
            # Advance one month.
            from dateutil.relativedelta import relativedelta
            current = current + relativedelta(months=1)
    try:
        create_installment_plan(invoice, rows,
                                  actor_id=current_user.id)
        flash("تم إنشاء خطة الأقساط", "success")
    except InstallmentError as e:
        flash(str(e), "error")
    return redirect(url_for("invoices.view",
                              invoice_id=invoice.id))


@bp.route("/installments/<int:installment_id>/pay", methods=["POST"])
@login_required
@require_permission("invoices.create")
def pay_installment_route(installment_id):
    from app.services.installments import (
        pay_installment, InstallmentError,
    )
    from app.models import InvoiceInstallment, PaymentMethod
    inst = db.session.get(InvoiceInstallment, installment_id)
    if not inst or inst.invoice.company_id != g.active_company.id:
        abort(404)
    pm_id = request.form.get("payment_method_id", type=int)
    pm = db.session.get(PaymentMethod, pm_id) if pm_id else None
    if not pm or pm.company_id != inst.invoice.company_id:
        flash("اختر طريقة دفع صحيحة", "error")
        return redirect(url_for("invoices.view",
                                  invoice_id=inst.invoice_id))
    try:
        pay_installment(inst, payment_method=pm,
                         actor_id=current_user.id)
        flash(f"تم تحصيل قسط {inst.amount}", "success")
    except InstallmentError as e:
        flash(str(e), "error")
    return redirect(url_for("invoices.view",
                              invoice_id=inst.invoice_id))


@bp.route("/<int:invoice_id>/installments/drop", methods=["POST"])
@login_required
@require_permission("invoices.create")
def drop_installments(invoice_id):
    """Delete the entire plan. Only allowed when no installment has
    been paid yet — otherwise the audit trail would be inconsistent
    with the invoice's paid_amount."""
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        abort(404)
    if any(i.status == "PAID" for i in invoice.installments):
        flash("لا يمكن حذف الخطة بعد تحصيل قسط", "error")
        return redirect(url_for("invoices.view",
                                  invoice_id=invoice.id))
    for i in list(invoice.installments):
        db.session.delete(i)
    db.session.commit()
    flash("تم حذف خطة الأقساط", "success")
    return redirect(url_for("invoices.view", invoice_id=invoice.id))


# MARSOUD-CUSTOMER-DEPOSIT-01 UI (Abdelhamid 2026-07-24) — apply an
# ACTIVE deposit against this invoice. The service handles cross-
# customer / cross-company / already-applied guards.
@bp.route("/<int:invoice_id>/apply-deposit", methods=["POST"])
@login_required
@require_permission("invoices.create")
def apply_deposit(invoice_id):
    from app.services.deposits import apply_to_invoice, DepositError
    from app.models import CustomerDeposit
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    deposit_id = request.form.get("deposit_id", type=int)
    if not deposit_id:
        flash("اختر عربوناً أولاً", "error")
        return redirect(url_for("invoices.view",
                                  invoice_id=invoice.id))
    d = db.session.get(CustomerDeposit, deposit_id)
    if not d or d.company_id != invoice.company_id:
        flash("العربون غير موجود", "error")
        return redirect(url_for("invoices.view",
                                  invoice_id=invoice.id))
    try:
        apply_to_invoice(d, invoice, actor_id=current_user.id)
        flash(f"تم خصم {d.amount} من العربون على الفاتورة", "success")
    except DepositError as e:
        flash(str(e), "error")
    return redirect(url_for("invoices.view",
                              invoice_id=invoice.id))


# MARSOUD-INVOICE-DELETE (Abdelhamid 2026-07-13) — the ticket asks for
# a "delete anywhere" action. Chose the void-with-reversing-entry
# variant (Option 1) because hard-deleting an issued invoice destroys
# the audit trail + can violate KSA/Egypt VAT law. Behaviour:
#   · DRAFT invoice (never posted a journal) → hard delete, safe.
#   · Any other status → issue a FULL refund which reverses AR/VAT/
#     revenue/cash, restocks inventory, claws back commission — then
#     mark voided_at + set status to VOIDED. Net accounting impact = 0,
#     audit trail preserved.
@bp.route("/<int:invoice_id>/delete", methods=["POST"])
@login_required
@require_permission("invoices.refund")
def delete(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    reason = (request.form.get("reason") or "").strip() or "حذف الفاتورة"
    try:
        if invoice.status == InvoiceStatus.DRAFT:
            # Never posted anywhere — items cascade via
            # cascade="all, delete-orphan" on the Invoice model.
            invoice_number = invoice.number
            db.session.delete(invoice)
            db.session.commit()
            flash(f"تم حذف الفاتورة {invoice_number}", "success")
            return redirect(url_for("invoices.index"))
        # Posted invoice — reverse via FULL refund, then void.
        if invoice.status in (InvoiceStatus.REFUNDED,
                              InvoiceStatus.VOIDED):
            flash("الفاتورة معكوسة/ملغاة بالفعل", "warning")
            return redirect(url_for("invoices.view",
                                     invoice_id=invoice_id))
        from app.models.refund import RefundType
        issue_refund(
            invoice, RefundType.FULL, reason=reason,
            created_by=current_user.id, notify=False,
        )
        # issue_refund() sets status=REFUNDED; we relabel to VOIDED
        # because the user's intent was "delete" not "customer wanted
        # a refund." The reversing entry stays either way.
        from datetime import datetime as _dt
        invoice.status = InvoiceStatus.VOIDED
        invoice.voided_at = _dt.utcnow()
        invoice.voided_by_id = current_user.id
        invoice.void_reason = reason
        db.session.commit()
        try:
            from app.services.superadmin import log_platform_action
            log_platform_action(
                "invoice_deleted",
                target_company_id=invoice.company_id,
                actor_id=current_user.id,
                details=f"#{invoice.number} reason={reason[:60]}")
        except Exception:
            pass
        flash(f"تم حذف الفاتورة {invoice.number} وعكس القيود المرتبطة بها",
              "success")
    except (LedgerError, KeyError) as e:
        flash(str(e), "error")
    return redirect(url_for("invoices.index"))


# MARSOUD-INVOICES-RESTORE-01 (Batch 8 Ticket 3, 2026-07-30) —
# Undo a soft-delete. Posts a compensating JE that reverses the
# refund/reversal the delete route created, so the customer's
# sub-account balance returns to its pre-delete value. Gated on
# invoices.refund (same permission as delete).
@bp.route("/<int:invoice_id>/restore", methods=["POST"])
@login_required
@require_permission("invoices.refund")
def restore(invoice_id):
    from app.models.journal import JournalEntry, JournalLine
    from app.services.ledger import post_journal
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    if invoice.voided_at is None:
        flash("الفاتورة نشطة بالفعل", "warning")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    # Find the most-recent reversal JE for this invoice. The
    # delete route calls issue_refund() which — for FULL refunds
    # on unpaid invoices (the delete case) — posts a JE with
    # source_type='refund' + source_id=invoice.id. The ORIGINAL
    # invoice posting is source_type='invoice'. We distinguish
    # the reversal by looking at the AR sub-account: original
    # DEBITED it, reversal CREDITED it.
    from app.services.subsidiary import party_ar_account
    ar_acc = party_ar_account(invoice)
    all_entries = (JournalEntry.query
                     .filter(JournalEntry.company_id == invoice.company_id,
                               JournalEntry.source_id == invoice.id,
                               JournalEntry.source_type.in_(
                                   ("invoice", "refund")))
                     .order_by(JournalEntry.id.asc())
                     .all())
    reversal = None
    for e in all_entries:
        for line in e.lines:
            if line.account_id == ar_acc.id and (line.credit or 0) > 0.01:
                reversal = e
                break
        if reversal:
            break
    if not reversal:
        flash("مفيش قيد عكسي مربوط بالفاتورة — تواصل مع الدعم",
              "error")
        return redirect(url_for("invoices.view",
                                 invoice_id=invoice_id))
    # Post a compensating JE that undoes the reversal (Dr what
    # was credited, Cr what was debited).
    comp_lines = []
    for line in reversal.lines:
        comp_lines.append({
            "account_id": line.account_id,
            "debit": float(line.credit or 0),
            "credit": float(line.debit or 0),
            "memo": (line.memo or "") + " — استرجاع",
        })
    try:
        post_journal(
            company_id=invoice.company_id,
            description=f"استرجاع فاتورة {invoice.number}",
            lines=comp_lines,
            entry_date=date.today(),
            reference=f"RESTORE-{invoice.number}",
            currency=invoice.currency,
            created_by=current_user.id,
            source_type="invoice",
            source_id=invoice.id,
        )
    except LedgerError as e:
        flash(f"تعذّر ترحيل قيد الاسترجاع: {e}", "error")
        return redirect(url_for("invoices.view",
                                 invoice_id=invoice_id))
    # Clear void state + recompute status from paid_amount.
    invoice.voided_at = None
    invoice.voided_by_id = None
    invoice.void_reason = None
    paid = float(invoice.paid_amount or 0)
    total = float(invoice.total or 0)
    if paid >= total - 0.01 and total > 0:
        invoice.status = InvoiceStatus.PAID
    elif paid > 0:
        invoice.status = InvoiceStatus.PARTIALLY_PAID
    elif invoice.due_date and invoice.due_date < date.today():
        invoice.status = InvoiceStatus.OVERDUE
    else:
        invoice.status = InvoiceStatus.SENT
    db.session.commit()
    try:
        from app.services.activity import log_action
        log_action(
            action_type="UPDATE", entity_type="invoice",
            entity_id=invoice.id,
            entity_label=f"استرجاع فاتورة {invoice.number}",
            company_id=invoice.company_id,
            extra_data={"restored": True,
                        "new_status": invoice.status.value},
        )
    except Exception:
        pass
    flash(f"تم استرجاع الفاتورة {invoice.number} بحالة "
          f"{invoice.status.value}", "success")
    return redirect(url_for("invoices.view", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/refund", methods=["POST"])
@login_required
@require_permission("invoices.refund")
def refund(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    try:
        rtype = RefundType[request.form.get("type")]
        amount = request.form.get("amount")
        reason = request.form.get("reason", "")
        notify = request.form.get("email_customer") == "1"
        issue_refund(invoice, rtype, amount=amount, reason=reason,
                     created_by=current_user.id, notify=notify)
        try:
            from app.services.superadmin import log_platform_action
            log_platform_action("invoice_refunded",
                                target_company_id=invoice.company_id,
                                actor_id=current_user.id,
                                details=f"#{invoice.number} reason={reason[:60]}")
        except Exception:
            pass
        flash("تم تسجيل الاسترداد", "success")
    except (LedgerError, KeyError) as e:
        flash(str(e), "error")
    return redirect(url_for("invoices.view", invoice_id=invoice_id))
