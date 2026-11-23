from datetime import datetime, date, timedelta
from flask import Blueprint, render_template, redirect, url_for, flash, request, g, send_file
from flask_login import login_required, current_user
from app import db
from app.models import (
    Invoice, InvoiceItem, InvoiceStatus, Customer, Payment, Product,
    PaymentMethod, DiscountType, CostCenter,
)
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


# MARSOUD-COST-CENTERS-03-REVENUE-SPLIT (2026-09-03) — same helpers
# shipped by MARSOUD-COST-CENTERS-02 on the vendor-bills side. Kept
# module-local per the codebase's per-file style so the two write
# paths never lock-step across releases.
def _company_cost_centers(company_id):
    return (CostCenter.query
            .filter_by(company_id=company_id, is_active=True)
            .filter(CostCenter.deleted_at.is_(None))
            .order_by(CostCenter.code).all())


def _pick_cc_at(lst, i, valid_cc_ids):
    """Coerce the i-th form entry to a validated CC id: int only when
    the id is a known active CC in this company, otherwise None.
    Blank / stray / cross-tenant ids collapse to None so a fat-finger
    never blocks the save. Belt: post_journal at ledger.py:91-97 is
    the second line of defence."""
    if i >= len(lst):
        return None
    raw = (lst[i] or "").strip()
    if not raw:
        return None
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        return None
    return cid if cid in valid_cc_ids else None


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
    serial_texts = form.getlist("item_serial_numbers[]")
    # MARSOUD-COST-CENTERS-03-REVENUE-SPLIT — optional CC per item.
    # Whitelist built once so a wide invoice = one query, not N.
    cost_center_ids = form.getlist("item_cost_center_id[]")
    _valid_cc_ids = {cc.id for cc
                     in _company_cost_centers(invoice.company_id)}

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
            serial_numbers_text=((serial_texts[i] if i < len(serial_texts) else "") or "").strip() or None,
            # MARSOUD-COST-CENTERS-03-REVENUE-SPLIT — optional CC
            # per line. NULL keeps the item in the "unclassified"
            # bucket; a valid id sends the split of the revenue
            # credit into that bucket when the invoice posts.
            cost_center_id=_pick_cc_at(cost_center_ids, i,
                                        _valid_cc_ids),
        )
        db.session.add(item)
    db.session.flush()
    invoice.items = InvoiceItem.query.filter_by(invoice_id=invoice.id).all()
    invoice.recalc()


# MARSOUD-INVOICE-INSTALLMENTS-DISPLAY-01 (2026-09-08) — up-front
# validation for the installment + down-payment form inputs. Runs
# BEFORE post_invoice_to_ledger commits so a bad input surfaces as a
# refusal, not an orphan invoice.
def _validate_installment_form(form, total, inv_currency, base_currency):
    def _fnum(x):
        try:
            return float((x or "").strip())
        except (TypeError, ValueError, AttributeError):
            return 0.0

    dp_amount = _fnum(form.get("down_payment_amount"))
    inst_count_raw = (form.get("installment_count") or "").strip()
    try:
        inst_count = int(inst_count_raw) if inst_count_raw else 0
    except (TypeError, ValueError):
        inst_count = 0
    inst_start_raw = (form.get("installment_start_date") or "").strip()
    # MARSOUD-INVOICE-CUSTOM-INSTALLMENTS-01 (2026-10-10) — detect
    # the custom-schedule mode and defer its validation to the
    # service-side `_validate_custom_rows` (which raises the exact
    # Arabic diff message).  Reach the equal-split branch below only
    # when the operator stayed on the default equal mode.
    mode = (form.get("installment_mode") or "equal").strip()
    if mode == "custom":
        from app.services.installments import (
            _parse_custom_rows, _validate_custom_rows, InstallmentError,
        )
        try:
            raw = _parse_custom_rows(form)
            _validate_custom_rows(raw, total, dp_amount, None)
            # issue_date is None here (the invoice object isn't
            # created yet at this validation point); the final
            # `_apply_create_form_installments` runs a second
            # _validate_custom_rows WITH `invoice.issue_date` after
            # the invoice row exists.
        except InstallmentError as e:
            raise LedgerError(str(e))
        if dp_amount > 0 and not (form.get("down_payment_method_id") or "").strip():
            raise LedgerError("اختر طريقة الدفع للدفعة المقدّمة")
        return

    # No installment section touched → nothing to validate.
    if inst_count < 2 and dp_amount <= 0:
        return

    if dp_amount < 0 or dp_amount > total + 0.005:
        raise LedgerError(
            f"الدفعة المقدّمة يجب أن تكون بين 0 و {total:.2f}")
    if dp_amount > 0 and not (form.get("down_payment_method_id") or "").strip():
        raise LedgerError("اختر طريقة الدفع للدفعة المقدّمة")
    if inst_count >= 2:
        if not inst_start_raw:
            raise LedgerError("تاريخ أول قسط مطلوب لخطة الأقساط")
        from datetime import datetime as _dt
        try:
            _dt.strptime(inst_start_raw, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise LedgerError("تاريخ أول قسط غير صالح")
        if (total - dp_amount) <= 0.005:
            raise LedgerError(
                "المبلغ المتبقّي بعد الدفعة المقدّمة يجب أن يكون أكبر من صفر")
    # Foreign-currency guard — down-payment on a non-base invoice needs
    # exchange_rate; the shared record_payment path enforces it too,
    # but surface a nicer message here rather than deep inside ledger.
    inv_ccy = (inv_currency or "").upper()
    base = (base_currency or "EGP").upper()
    if dp_amount > 0 and inv_ccy != base:
        fx_raw = (form.get("exchange_rate") or "").strip()
        try:
            fx = float(fx_raw) if fx_raw else 0.0
        except (TypeError, ValueError):
            fx = 0.0
        if fx <= 0:
            raise LedgerError(
                "الفاتورة بعملة أجنبية — سعر الصرف مطلوب لتحصيل الدفعة "
                "المقدّمة")


# MARSOUD-INVOICE-INSTALLMENTS-DISPLAY-01 (2026-09-08) — create-form
# extras: installment plan + up-front payment. Extracted from the
# `new()` post-branch so the flow stays legible + so the audit can
# exercise this side of the logic without POSTing a full form.
def _apply_create_form_installments(invoice, form, actor_id):
    """Apply the optional installment plan + down-payment captured on
    the create form. Called only after `post_invoice_to_ledger` (so
    the AR row exists) and only on the send path (draft/quote path
    doesn't wire installments — the ticket asks for one-shot creation
    + activation, not a deferred plan). All-nothing: any error rolls
    the whole invoice back."""
    from app.services.installments import (
        create_installment_plan, InstallmentError,
    )
    from datetime import datetime as _dt

    def _fnum(x):
        try:
            return float((x or "").strip())
        except (TypeError, ValueError, AttributeError):
            return 0.0

    dp_amount = _fnum(form.get("down_payment_amount"))
    dp_method_id = form.get("down_payment_method_id") or ""
    try:
        dp_method_id = int(dp_method_id) if dp_method_id else None
    except (TypeError, ValueError):
        dp_method_id = None
    inst_count_raw = (form.get("installment_count") or "").strip()
    try:
        inst_count = int(inst_count_raw) if inst_count_raw else 0
    except (TypeError, ValueError):
        inst_count = 0
    inst_start_raw = (form.get("installment_start_date") or "").strip()
    # MARSOUD-INVOICE-CUSTOM-INSTALLMENTS-01 (2026-10-10) — detect
    # the mode BEFORE the "nothing touched" early-return.  In custom
    # mode the operator doesn't fill `installment_count` (that's the
    # equal-mode field), so without this hoist the function would
    # bail out and the custom rows would silently vanish.
    mode = (form.get("installment_mode") or "equal").strip()

    # Nothing on this section → return silently (regular sale path).
    # Custom mode is "touched" whenever the operator switched to it,
    # even without a down-payment — don't short-circuit there.
    if mode != "custom" and inst_count < 2 and dp_amount <= 0:
        return

    total = float(invoice.total or 0)
    if dp_amount < 0 or dp_amount > total + 0.005:
        raise LedgerError(
            f"الدفعة المقدّمة يجب أن تكون بين 0 و {total:.2f}")

    # ─── Down-payment first — record_payment bumps invoice.paid_amount
    # so the plan validation (which now compares sum-of-rows to the
    # REMAINING balance) sees the correct target when it runs. ──────
    if dp_amount > 0:
        if not dp_method_id:
            raise LedgerError(
                "اختر طريقة الدفع للدفعة المقدّمة")
        invoice.down_payment_amount = dp_amount
        # exchange_rate forwarded when a foreign-currency invoice is
        # in play; blank/None → EGP path unchanged.
        _fx_raw = (form.get("exchange_rate") or "").strip()
        try:
            _fx = float(_fx_raw) if _fx_raw else None
        except (TypeError, ValueError):
            _fx = None
        record_payment(invoice, dp_amount,
                        payment_method_id=dp_method_id,
                        created_by=actor_id, notify=False,
                        exchange_rate=_fx)

    # ─── Installment plan (optional) ─────────────────────────────────
    # The radio toggle "installment_mode = equal | custom" was read
    # above (before the early-return) so it also gates whether we even
    # reach this point.  Default stays `equal`, so every pre-existing
    # POST (which doesn't send the field) takes the untouched legacy
    # path below.
    rows = None
    if mode == "custom":
        from app.services.installments import (
            _parse_custom_rows, _validate_custom_rows,
        )
        try:
            raw = _parse_custom_rows(form)
            rows = _validate_custom_rows(
                raw, total, dp_amount, invoice.issue_date)
        except InstallmentError as e:
            raise LedgerError(str(e))
    elif inst_count >= 2:
        if not inst_start_raw:
            raise LedgerError("تاريخ أول قسط مطلوب لخطة الأقساط")
        try:
            start_date = _dt.strptime(inst_start_raw, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise LedgerError("تاريخ أول قسط غير صالح")
        remaining = total - dp_amount
        if remaining <= 0.005:
            raise LedgerError(
                "المبلغ المتبقّي بعد الدفعة المقدّمة يجب أن يكون أكبر من صفر")
        per = round(remaining / inst_count, 2)
        rows = []
        due = start_date
        acc = 0.0
        for i in range(inst_count):
            if i == inst_count - 1:
                amt = round(remaining - acc, 2)
            else:
                amt = per
                acc += amt
            rows.append({"amount": amt, "due_date": due})
            # Monthly step matching the /invoices/<id>/installments
            # panel: +30 days.  The service accepts any date sequence.
            due = due.fromordinal(due.toordinal() + 30)
    if rows:
        try:
            create_installment_plan(invoice, rows, actor_id=actor_id)
        except InstallmentError as e:
            raise LedgerError(str(e))
        # MARSOUD-INVOICE-INSTALLMENT-EMAILS-01 (2026-09-08) — send
        # the "invoice created + here's the schedule" email the
        # moment the plan lands. Non-blocking — a mail failure must
        # not roll back the invoice. Respects the same email_customer
        # checkbox that gates the regular /invoices/new notification.
        if (form.get("email_customer") == "1"
                and invoice.customer and invoice.customer.email):
            try:
                from app.services.email import send_installment_email
                send_installment_email(invoice, kind="plan_created")
            except Exception:
                import logging
                logging.getLogger("ledgeros.invoicing").exception(
                    "Failed to send installment plan_created email "
                    "for invoice %s", invoice.number)


# MARSOUD-INVOICE-CUSTOM-INSTALLMENTS-01 (2026-10-10) — save-as-quote
# bridge.  The create form's `/send` branch runs
# `_apply_create_form_installments` to BUILD the plan immediately;
# the save-as-quote branch (`should_send=False`) instead calls this
# helper, which validates the plan AND serializes it onto
# `invoice.pending_plan_json`.  A later `/invoices/<id>/send` picks
# it up via `apply_pending_plan` and converts the quote without
# re-validating.
def _persist_quote_plan(invoice, form):
    """If the operator chose "جدول مخصص" on the save-as-quote path,
    validate + serialize the custom schedule onto
    `invoice.pending_plan_json` so a subsequent /send call applies it
    without re-entering.  No-op for equal-mode or empty card —
    equal-split plans on save-as-quote are discarded exactly as they
    were before this ticket, keeping behavior narrowly scoped to the
    "جدول مخصص" case the ticket actually asked for.
    """
    from app.services.installments import (
        _parse_custom_rows, _validate_custom_rows, serialize_pending_plan,
        InstallmentError,
    )

    def _fnum(x):
        try:
            return float((x or "").strip())
        except (TypeError, ValueError, AttributeError):
            return 0.0

    mode = (form.get("installment_mode") or "equal").strip()
    if mode != "custom":
        return  # equal mode / empty card — nothing new to persist

    dp_amount = _fnum(form.get("down_payment_amount"))
    dp_method_id = (form.get("down_payment_method_id") or "").strip() or None
    total = float(invoice.total or 0)

    try:
        raw = _parse_custom_rows(form)
        rows = _validate_custom_rows(
            raw, total, dp_amount, invoice.issue_date)
    except InstallmentError as e:
        raise LedgerError(str(e))
    if dp_amount > 0 and not dp_method_id:
        raise LedgerError("اختر طريقة الدفع للدفعة المقدّمة")
    invoice.pending_plan_json = serialize_pending_plan(
        "custom", rows, dp_amount, dp_method_id)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("invoices.create")
def new():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    customers = Customer.query.filter_by(company_id=g.active_company.id, is_active=True).order_by(Customer.name).all()
    # MARSOUD-TKT-INVOICE-INLINE-CUSTOMER — the quick-add modal's
    # "sales rep" dropdown needs the same list customers/new uses. We
    # import here to keep the reps-only import off the module top level.
    from app.routes.customers import _company_reps
    reps = _company_reps()
    if request.method == "POST":
        try:
            # MARSOUD-INVOICE-FX-01 — currency picked on the form.
            # Defaults to the company's base currency when the user
            # didn't touch the picker (or the picker isn't rendered).
            # Whitelisted against _ALLOWED_INVOICE_CURRENCIES so a
            # hand-crafted POST can't inject an unsupported code.
            _picked = (request.form.get("currency") or "").strip().upper()
            _picked = _picked if _picked in _ALLOWED_INVOICE_CURRENCIES else None
            _inv_currency = _picked or g.active_company.base_currency
            invoice = Invoice(
                company_id=g.active_company.id,
                number=_next_number(g.active_company.id),
                customer_id=int(request.form.get("customer_id")),
                issue_date=date.today(),
                due_date=date.today() + timedelta(days=30),
                currency=_inv_currency,
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
                # MARSOUD-INVOICE-INSTALLMENTS-DISPLAY-01 (2026-09-08) —
                # validate the installment/down-payment inputs BEFORE
                # the ledger post commits.  Once post_invoice_to_ledger
                # runs its own db.session.commit() the invoice is
                # persisted for real, so a later LedgerError would
                # leave an orphan invoice.  Fail-fast up front.
                _validate_installment_form(
                    request.form, float(invoice.total or 0),
                    invoice.currency,
                    (g.active_company.base_currency or "EGP"))
                invoice.status = InvoiceStatus.SENT
                post_invoice_to_ledger(invoice, created_by=current_user.id)
                # Now the AR row exists — apply the plan + down-payment.
                _apply_create_form_installments(
                    invoice, request.form, current_user.id)
            else:
                # MARSOUD-INVOICE-CUSTOM-INSTALLMENTS-01 (2026-10-10) —
                # Save-as-quote path.  If the operator authored any
                # installments plan (custom OR equal mode), serialize
                # it onto `invoice.pending_plan_json` so a subsequent
                # /send call can apply it without the operator having
                # to re-enter the schedule.  Validates the plan here
                # so a broken schedule fails on quote-save, not later
                # on send.  No stored plan when the installments card
                # was left untouched.
                _persist_quote_plan(invoice, request.form)
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
                # MARSOUD-INVOICE-INSTALLMENT-EMAILS-01 (2026-09-08)
                # — when the invoice ships with a plan, the plan_
                # created email was already sent from inside
                # _apply_create_form_installments and carries the
                # schedule + timeline the customer needs. Sending
                # the generic invoice_sent on top would double-notify
                # for the same event.  Suppress in that case.
                if not invoice.installments:
                    send_invoice_notification(invoice)
            flash(f"تم إنشاء الفاتورة {invoice.number}", "success")
            return redirect(url_for("invoices.view", invoice_id=invoice.id))
        except LedgerError as e:
            db.session.rollback()
            flash(str(e), "error")
        except Exception as e:
            db.session.rollback()
            flash(f"خطأ: {e}", "error")

    return render_template("invoices/form.html", customers=customers,
                             invoice=None, reps=reps,
                             # MARSOUD-COST-CENTERS-03-REVENUE-SPLIT
                             cost_centers=_company_cost_centers(
                                 g.active_company.id),
                             # MARSOUD-INVOICE-FX-01 — feeds the
                             # currency picker on the create form.
                             allowed_currencies=_ALLOWED_INVOICE_CURRENCIES,
                             # MARSOUD-INVOICE-INSTALLMENTS-DISPLAY-01
                             # — down-payment method picker on the
                             # installment card. Filtered to active
                             # so a disabled channel doesn't show up.
                             payment_methods=(
                                 PaymentMethod.query
                                 .filter_by(company_id=g.active_company.id,
                                             is_active=True)
                                 .order_by(PaymentMethod.is_default.desc(),
                                            PaymentMethod.name).all()))


@bp.route("/<hid:invoice_id>/edit", methods=["GET", "POST"])
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
    # MARSOUD-TKT-INVOICE-INLINE-CUSTOMER — same rationale as `new`.
    from app.routes.customers import _company_reps
    reps = _company_reps()
    if request.method == "POST":
        try:
            _populate_invoice_from_form(invoice, request.form)
            db.session.commit()
            flash("تم حفظ التعديلات", "success")
            return redirect(url_for("invoices.view", invoice_id=invoice.id))
        except Exception as e:
            db.session.rollback()
            flash(f"خطأ: {e}", "error")

    return render_template("invoices/form.html", customers=customers,
                             invoice=invoice, reps=reps,
                             # MARSOUD-COST-CENTERS-03-REVENUE-SPLIT
                             cost_centers=_company_cost_centers(
                                 g.active_company.id),
                             # MARSOUD-INVOICE-FX-01 — passed for
                             # symmetry; the template hides the
                             # picker on the edit path (currency is
                             # frozen once created — use the separate
                             # /change_currency route to relabel).
                             allowed_currencies=_ALLOWED_INVOICE_CURRENCIES)


# ─── MARSOUD-CURRENCY-TAX-DEFAULTS (Batch 8 Ticket 4c, 2026-07-30) ──
_ALLOWED_INVOICE_CURRENCIES = ("EGP", "SAR", "AED", "USD", "EUR")


@bp.route("/<hid:invoice_id>/change-currency", methods=["POST"])
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


@bp.route("/<hid:invoice_id>/preview")
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


@bp.route("/<hid:invoice_id>")
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


@bp.route("/<hid:invoice_id>/send", methods=["POST"])
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
            # MARSOUD-INVOICE-CUSTOM-INSTALLMENTS-01 (2026-10-10) — if
            # the operator authored an installments plan at quote time
            # (`invoice.pending_plan_json` is set), apply it NOW without
            # re-validating.  The plan was validated at save-as-quote
            # time; the ticket's "يتحول لفاتورة عند الإرسال بدون ما
            # يتفقد" rule.  Clears the stored blob on success.  No-op
            # for every existing invoice (NULL column).
            from app.services.installments import (
                apply_pending_plan, InstallmentError,
            )
            try:
                had_plan = bool(invoice.pending_plan_json)
                apply_pending_plan(invoice, actor_id=current_user.id)
            except InstallmentError as e:
                raise LedgerError(str(e))
            if request.form.get("email_customer", "1") == "1":
                # Same no-double-notify pattern as `new()` POST
                # at :484-493: when a plan was applied, send the
                # schedule-aware `plan_created` email; otherwise
                # the generic `invoice_sent`.
                if had_plan:
                    if invoice.customer and invoice.customer.email:
                        try:
                            from app.services.email import send_installment_email
                            send_installment_email(invoice, kind="plan_created")
                        except Exception:
                            pass
                else:
                    send_invoice_notification(invoice)
            flash("تم إرسال الفاتورة وتسجيل القيد", "success")
        except LedgerError as e:
            flash(str(e), "error")
    return redirect(url_for("invoices.view", invoice_id=invoice_id))


@bp.route("/<hid:invoice_id>/resend", methods=["POST"])
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


@bp.route("/<hid:invoice_id>/send-overdue-reminder", methods=["POST"])
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


@bp.route("/<hid:invoice_id>/pay", methods=["POST"])
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
        # MARSOUD-INVOICE-FX-01 — optional; ignored for base-currency
        # invoices, required for foreign ones (service enforces).
        fx_raw = (request.form.get("exchange_rate") or "").strip()
        exchange_rate = _safe_float(fx_raw, 0) if fx_raw else None
        if exchange_rate == 0:
            exchange_rate = None
        record_payment(
            invoice, amount,
            payment_method_id=int(pmid) if pmid else None,
            created_by=current_user.id, notify=notify,
            exchange_rate=exchange_rate,
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
@bp.route("/<hid:invoice_id>/installments/plan", methods=["POST"])
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


@bp.route("/installments/<hid:installment_id>/pay", methods=["POST"])
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


@bp.route("/<hid:invoice_id>/installments/drop", methods=["POST"])
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
@bp.route("/<hid:invoice_id>/apply-deposit", methods=["POST"])
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
@bp.route("/<hid:invoice_id>/delete", methods=["POST"])
@login_required
@require_permission("invoices.refund")
def delete(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    # MARSOUD-INVOICE-CANCEL-EMAIL-01 (2026-10-18) — ticket makes the
    # reason mandatory ("لا يمكن تأكيد الإلغاء دون إدخال السبب") so
    # the audit trail is always populated AND the notification email
    # has something meaningful to show the customer.  No more silent
    # "حذف الفاتورة" fallback.
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("سبب الإلغاء مطلوب لإتمام العملية", "error")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    invoice_number = invoice.number
    invoice_company_id = invoice.company_id
    # MARSOUD-TKT-ADMIN-VOID-SAAS-INVOICE — logic moved to
    # services/invoicing.void_invoice so the super-admin shortcut
    # from /admin/saas can share it byte-for-byte. Behaviour here
    # is unchanged.
    from app.services.invoicing import void_invoice
    try:
        outcome = void_invoice(invoice, reason, current_user.id)
    except RuntimeError as e:
        flash(str(e), "warning")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    except (LedgerError, KeyError) as e:
        flash(str(e), "error")
        return redirect(url_for("invoices.index"))
    try:
        from app.services.superadmin import log_platform_action
        log_platform_action(
            "invoice_deleted",
            target_company_id=invoice_company_id,
            actor_id=current_user.id,
            details=f"#{invoice_number} reason={reason[:60]}")
    except Exception:
        pass
    if outcome == "deleted":
        flash(f"تم حذف الفاتورة {invoice_number}", "success")
    else:
        flash(f"تم حذف الفاتورة {invoice_number} وعكس القيود المرتبطة بها",
              "success")
    return redirect(url_for("invoices.index"))


# MARSOUD-INVOICES-RESTORE-01 (Batch 8 Ticket 3, 2026-07-30) —
# Undo a soft-delete. Posts a compensating JE that reverses the
# refund/reversal the delete route created, so the customer's
# sub-account balance returns to its pre-delete value. Gated on
# invoices.refund (same permission as delete).
@bp.route("/<hid:invoice_id>/restore", methods=["POST"])
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
    # MARSOUD-INVOICE-CANCEL-EMAIL-01 (2026-10-18) — clear the
    # cancel-notification idempotency stamp too, so a future re-void
    # of this restored invoice is treated as a NEW business event
    # that re-notifies the customer.  Leaving the stamp in place
    # would silently suppress the second notification.
    invoice.cancel_email_sent_at = None
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


# MARSOUD-INVOICE-CANCEL-EMAIL-01 (2026-10-18) — retry endpoint for
# a cancel notification that failed on the first attempt (SMTP down,
# transient DNS, customer-email-added-after-the-fact).  Idempotent:
# a second POST after a successful send finds `cancel_email_sent_at`
# already stamped and flashes an "already sent" notice instead of
# firing a duplicate.
@bp.route("/<hid:invoice_id>/resend-cancel-email", methods=["POST"])
@login_required
@require_permission("invoices.refund")
def resend_cancel_email(invoice_id):
    invoice = db.session.get(Invoice, invoice_id)
    if not invoice or invoice.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("invoices.index"))
    if invoice.status != InvoiceStatus.VOIDED:
        flash("إعادة الإرسال متاحة فقط للفواتير الملغاة", "warning")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    if invoice.cancel_email_sent_at is not None:
        flash("تم إرسال الإشعار مسبقاً — لا حاجة لإعادة الإرسال",
              "info")
        return redirect(url_for("invoices.view", invoice_id=invoice_id))
    from app.services.email import send_invoice_cancelled_email
    from datetime import datetime as _dt
    if send_invoice_cancelled_email(invoice):
        invoice.cancel_email_sent_at = _dt.utcnow()
        db.session.commit()
        flash("تم إرسال إشعار الإلغاء بنجاح", "success")
    else:
        flash("فشل إرسال الإشعار — راجع السجلات أو تواصل مع الدعم",
              "error")
    return redirect(url_for("invoices.view", invoice_id=invoice_id))


@bp.route("/<hid:invoice_id>/refund", methods=["POST"])
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


# ─── MARSOUD-P126: External Platforms (Schools / Stores) Invoice Webhook ──
@bp.route("/integrations", methods=["GET", "POST"])
@login_required
def invoice_integrations():
    from flask import abort
    from app.models import InvoiceIntegration
    from app.services.permissions import get_user_role
    cid = g.active_company.id
    role = get_user_role(current_user.id, cid)
    if role not in ("owner", "admin", "ceo", "accountant"):
        abort(403)
    cfg = InvoiceIntegration.get_or_create(cid)
    if request.method == "POST":
        cfg.is_active = request.form.get("is_active") == "1"
        raw_pm = request.form.get("default_payment_method_id") or ""
        cfg.default_payment_method_id = int(raw_pm) if raw_pm.isdigit() else None
        cfg.default_tax_rate = max(0.0, min(100.0, _safe_float(request.form.get("default_tax_rate"), 15.0)))
        cfg.auto_post_ledger = request.form.get("auto_post_ledger") == "1"
        db.session.commit()
        flash("تم حفظ إعدادات ربط فواتير المنصات الخارجية.", "success")
        return redirect(url_for("invoices.invoice_integrations"))

    pms = PaymentMethod.query.filter_by(company_id=cid, is_active=True).order_by(PaymentMethod.id).all()
    webhook_url = request.url_root.rstrip("/") + url_for("invoices.invoice_webhook", secret=cfg.webhook_secret)
    return render_template(
        "invoices/integrations.html",
        cfg=cfg,
        payment_methods=pms,
        webhook_url=webhook_url,
    )


@bp.route("/webhook/<secret>", methods=["POST"])
def invoice_webhook(secret):
    import re
    from flask import jsonify, abort
    from app.models import InvoiceIntegration, Company
    from app.models.user import user_companies
    from app.services.opsflow_extras import notify

    cfg = InvoiceIntegration.query.filter_by(webhook_secret=secret).first()
    if not cfg:
        abort(404)
    if not cfg.is_active:
        return jsonify({"ok": False, "error": "integration_disabled"}), 403

    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid_payload"}), 400

    cid = cfg.company_id
    company = Company.query.get(cid)
    ext_id = str(payload.get("external_id") or payload.get("invoice_id") or payload.get("order_id") or "").strip()[:80]
    platform = str(payload.get("platform") or payload.get("source") or "منصة خارجية").strip()[:80]

    # 1) Idempotency guard via po_reference = EXT:<external_id>
    if ext_id:
        ref_tag = f"EXT:{ext_id}"
        existing = Invoice.query.filter_by(company_id=cid, po_reference=ref_tag).first()
        if existing:
            return jsonify({
                "ok": True,
                "deduplicated": True,
                "invoice_id": existing.id,
                "invoice_number": existing.number,
            })
    else:
        ref_tag = None

    # 2) Resolve or auto-create Customer
    cust_name = str(payload.get("customer_name") or payload.get("parent_name") or payload.get("client_name") or "").strip()[:150]
    cust_phone_raw = str(payload.get("customer_phone") or payload.get("phone") or payload.get("mobile") or "").strip()
    cust_phone = re.sub(r"[^\d+]", "", cust_phone_raw)[:30]
    cust_email = str(payload.get("customer_email") or payload.get("email") or "").strip().lower()[:150] or None

    if not cust_name and not cust_phone:
        return jsonify({"ok": False, "error": "customer_name_or_phone_required"}), 400
    if not cust_name:
        cust_name = f"عميل {cust_phone}"

    customer = None
    digits = re.sub(r"\D", "", cust_phone)
    if len(digits) >= 8:
        customer = Customer.query.filter(
            Customer.company_id == cid,
            Customer.phone.ilike(f"%{digits[-9:]}%"),
        ).first()
    if not customer:
        customer = Customer.query.filter_by(company_id=cid, name=cust_name).first()
    if not customer:
        customer = Customer(
            company_id=cid,
            name=cust_name,
            phone=cust_phone or None,
            email=cust_email,
            is_active=True,
        )
        db.session.add(customer)
        db.session.flush()

    # 3) Parse dates, currency, tax & line items
    def _parse_d(raw_d, fallback):
        if not raw_d:
            return fallback
        try:
            return date.fromisoformat(str(raw_d)[:10])
        except Exception:
            return fallback

    issue_dt = _parse_d(payload.get("issue_date"), date.today())
    due_dt = _parse_d(payload.get("due_date"), issue_dt)
    currency = str(payload.get("currency") or (company.base_currency if company else "SAR") or "SAR").strip().upper()[:3]
    tax_rate = float(cfg.default_tax_rate) if payload.get("tax_rate") is None else max(0.0, _safe_float(payload.get("tax_rate"), 0.0))

    raw_items = payload.get("items")
    parsed_items = []
    if isinstance(raw_items, list) and raw_items:
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            desc = str(it.get("description") or it.get("name") or it.get("title") or "بند من منصة خارجية").strip()[:255]
            qty = max(0.001, _safe_float(it.get("quantity") or it.get("qty"), 1.0))
            price = max(0.0, _safe_float(it.get("unit_price") or it.get("price") or it.get("amount"), 0.0))
            parsed_items.append((desc, qty, price))
    else:
        desc = str(payload.get("description") or payload.get("item_name") or f"فاتورة {platform}").strip()[:255]
        qty = max(0.001, _safe_float(payload.get("quantity"), 1.0))
        price = max(0.0, _safe_float(payload.get("unit_price") or payload.get("amount") or payload.get("total"), 0.0))
        if price > 0:
            parsed_items.append((desc, qty, price))

    if not parsed_items:
        return jsonify({"ok": False, "error": "valid_items_or_amount_required"}), 400

    owner_row = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == cid)
    ).first()
    actor_id = owner_row.user_id if owner_row else None

    inv_num = _next_number(cid)
    inv = Invoice(
        company_id=cid,
        number=inv_num,
        customer_id=customer.id,
        issue_date=issue_dt,
        due_date=due_dt,
        currency=currency,
        tax_rate=tax_rate,
        status=InvoiceStatus.DRAFT,
        source="WEBHOOK",
        po_reference=ref_tag,
        notes=str(payload.get("notes") or f"مستلمة تلقائياً من {platform}").strip()[:1000],
        created_by_id=actor_id,
    )
    db.session.add(inv)
    db.session.flush()

    for desc, qty, price in parsed_items:
        db.session.add(InvoiceItem(
            invoice_id=inv.id,
            company_id=cid,
            description=desc,
            quantity=qty,
            unit_price=price,
        ))
    db.session.flush()
    inv.recalc()

    # 4) Post ledger entry & optional automatic payment collection
    is_paid = str(payload.get("paid") or "").lower() in ("1", "true", "yes") or str(payload.get("status") or "").upper() == "PAID"
    paid_amt = _safe_float(payload.get("paid_amount"), float(inv.total) if is_paid else 0.0)

    try:
        if cfg.auto_post_ledger:
            inv.status = InvoiceStatus.SENT
            post_invoice_to_ledger(inv, created_by=actor_id)
            if paid_amt > 0.009:
                pm_id = cfg.default_payment_method_id
                if not pm_id:
                    pm = PaymentMethod.query.filter_by(company_id=cid, is_active=True).order_by(PaymentMethod.id).first()
                    pm_id = pm.id if pm else None
                record_payment(
                    inv,
                    min(paid_amt, float(inv.total)),
                    payment_date=issue_dt,
                    payment_method_id=pm_id,
                    created_by=actor_id,
                    notify=False,
                )
        cfg.invoices_received = (cfg.invoices_received or 0) + 1
        cfg.last_received_at = datetime.utcnow()
        cfg.last_error = None
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        cfg2 = InvoiceIntegration.query.filter_by(company_id=cid).first()
        if cfg2:
            cfg2.last_error = str(exc)[:350]
            db.session.commit()
        return jsonify({"ok": False, "error": str(exc)[:200]}), 400

    if actor_id:
        try:
            notify(
                actor_id,
                company_id=cid,
                kind="NEW_LEAD",
                title=f"🧾 فاتورة خارجية ({platform}): {inv.number}",
                body=f"العميل: {customer.name} · الإجمالي: {float(inv.total):,.2f} {inv.currency}",
                link_url=f"/invoices/{inv.id}",
            )
        except Exception:
            pass

    return jsonify({
        "ok": True,
        "invoice_id": inv.id,
        "invoice_number": inv.number,
        "customer_id": customer.id,
        "total": float(inv.total),
        "status": inv.status.value,
    })
