"""MARSOUD-LOYALTY-POINTS-01 (2026-09-02) — loyalty points service.

Every mutation to `Customer.loyalty_points_balance` funnels through
`_apply_points_delta` — one place to see, one place to audit. Same
discipline as `services/purchase_orders._apply_bill_to_po` etc.

Public functions:
  * `award_points_for_invoice(invoice, actor_id=None)` — call from
    `record_payment` on first full-paid transition. Idempotent
    (safe to call twice on the same invoice).
  * `redeem_points(invoice, points_requested, actor_id=None)` —
    called from POS BEFORE the JE is posted. Sets FIXED discount +
    recalc, records the clamped points count.
  * `reverse_points_for_invoice(invoice, actor_id=None)` — called
    from `void_pos_order`. Two independent branches (earn claw
    back / redeem refund).
  * `adjust_points_manually(customer, delta, reason_note, actor_id)`
    — owner/admin manual correction path.

Never touches `Invoice.recalc()`, `post_invoice_to_ledger`,
`inventory.py`, or `ledger.py`. Redemption uses the existing
FIXED-discount lane.
"""
from datetime import datetime
from app import db
from app.models import (
    Customer, LoyaltyPointTransaction, LoyaltyReason,
)


class LoyaltyError(Exception):
    """Domain-level validation failure. Route catches and flashes."""


# ─── Single mutation gate ───────────────────────────────────
def _apply_points_delta(customer, delta, reason, *, source_type=None,
                        source_id=None, actor_id=None,
                        reason_note=None):
    """Apply a signed delta to the customer's cached balance and
    persist a matching LoyaltyPointTransaction row in the same
    session. Refuses to let the balance go negative.
    """
    delta = int(delta)
    if delta == 0:
        return None
    new_balance = int(customer.loyalty_points_balance or 0) + delta
    if new_balance < 0:
        raise LoyaltyError(
            "رصيد النقاط لا يمكن أن يكون سالبًا")
    customer.loyalty_points_balance = new_balance
    txn = LoyaltyPointTransaction(
        company_id=customer.company_id,
        customer_id=customer.id,
        points_delta=delta,
        reason=reason,
        source_type=source_type,
        source_id=source_id,
        balance_after=new_balance,
        reason_note=reason_note,
        actor_id=actor_id,
    )
    db.session.add(txn)
    return txn


# ─── Earn ───────────────────────────────────────────────────
def award_points_for_invoice(invoice, actor_id=None):
    """Called from record_payment when the invoice becomes fully
    PAID for the first time. Idempotent: the second call is a
    no-op guarded by `loyalty_points_awarded_at`.
    """
    company = invoice.company
    if not company or not getattr(company, "loyalty_enabled", False):
        return
    if invoice.loyalty_points_awarded_at is not None:
        return   # already awarded — no double-earn
    if not invoice.customer_id:
        # Walk-in — no account to credit. Stamp the guard anyway so
        # we don't keep evaluating on later payments to the same
        # invoice (shouldn't happen, but cheap belt-and-braces).
        invoice.loyalty_points_awarded_at = datetime.utcnow()
        return
    rate = float(company.loyalty_earn_rate or 0)
    if rate <= 0:
        invoice.loyalty_points_awarded_at = datetime.utcnow()
        return
    base = float(invoice.taxable_base or 0)
    points = int(base // rate)
    if points <= 0:
        invoice.loyalty_points_awarded_at = datetime.utcnow()
        return
    customer = invoice.customer
    if not customer or customer.company_id != invoice.company_id:
        invoice.loyalty_points_awarded_at = datetime.utcnow()
        return
    _apply_points_delta(
        customer, points, LoyaltyReason.EARNED,
        source_type="invoice", source_id=invoice.id, actor_id=actor_id,
    )
    invoice.loyalty_points_earned = points
    invoice.loyalty_points_awarded_at = datetime.utcnow()
    # Callers embed us in a bigger flow that will commit anyway, but
    # the standalone paths (audit, direct route) need to see the row
    # land — flushing is enough; a caller's rollback still wipes us.
    db.session.flush()


# ─── Redeem ─────────────────────────────────────────────────
def redeem_points(invoice, points_requested, actor_id=None):
    """Called from POS BEFORE `post_invoice_to_ledger`. Sets
    invoice_discount_type = FIXED, invoice_discount_value = cash
    value, calls `invoice.recalc()` so the JE + receipt fold the
    redemption in naturally. Records the ACTUAL points consumed
    after the invoice-side `_resolve_discount` clamps to items_total.
    """
    company = invoice.company
    if not company or not getattr(company, "loyalty_enabled", False):
        raise LoyaltyError("برنامج الولاء غير مفعّل لهذه الشركة")
    if not invoice.customer_id:
        raise LoyaltyError(
            "لا يمكن صرف نقاط على فاتورة بدون عميل محدد")
    customer = invoice.customer
    if not customer or customer.company_id != invoice.company_id:
        raise LoyaltyError("العميل غير موجود")

    # Refuse mixing with an existing manual discount (ticket §2-ج).
    dtype = getattr(invoice.invoice_discount_type, "value", None)
    if dtype and dtype != "NONE" \
            and float(invoice.invoice_discount_value or 0) > 0:
        raise LoyaltyError(
            "الفاتورة عليها خصم يدوي بالفعل — لا يمكن الجمع بينه "
            "وبين صرف النقاط في نفس الفاتورة")

    try:
        points_requested = int(points_requested)
    except (TypeError, ValueError):
        raise LoyaltyError("عدد النقاط غير صالح") from None
    if points_requested <= 0:
        raise LoyaltyError("عدد النقاط يجب أن يكون أكبر من صفر")
    if points_requested > int(customer.loyalty_points_balance or 0):
        raise LoyaltyError(
            f"رصيد العميل {customer.loyalty_points_balance} نقطة فقط")

    unit = float(company.loyalty_redemption_value or 0)
    if unit <= 0:
        raise LoyaltyError(
            "قيمة النقطة عند الصرف غير مضبوطة — راجع إعدادات الولاء")
    value = round(points_requested * unit, 2)

    from app.models.invoice import DiscountType
    invoice.invoice_discount_type = DiscountType.FIXED
    invoice.invoice_discount_value = value
    invoice.recalc()   # _resolve_discount clamps automatically

    # Record the actual points consumed after any clamp.
    actual_value = float(invoice.invoice_discount_amount or 0)
    actual_points = int(round(actual_value / unit)) if unit else 0
    if actual_points <= 0:
        return
    _apply_points_delta(
        customer, -actual_points, LoyaltyReason.REDEEMED,
        source_type="invoice", source_id=invoice.id, actor_id=actor_id,
    )
    invoice.loyalty_points_redeemed = actual_points
    db.session.flush()


# ─── Reversal on void ───────────────────────────────────────
def reverse_points_for_invoice(invoice, actor_id=None):
    """Called from `void_pos_order`. Two independent branches:
      * Any redeemed points → return to the customer (REDEEMED_REFUNDED).
      * Any earned points   → claw back (EARNED_REVERSED). Best-effort:
        if the customer has since spent them elsewhere, the delta is
        recorded as a negative-that-would-clamp — we log a warning
        via reason_note and cap at the current balance rather than
        breaking the void.
    """
    if not invoice or not invoice.customer_id:
        return
    customer = invoice.customer
    if not customer:
        return

    # Refund redeemed points first (always safe — adding to balance).
    if int(invoice.loyalty_points_redeemed or 0) > 0:
        try:
            _apply_points_delta(
                customer, int(invoice.loyalty_points_redeemed),
                LoyaltyReason.REDEEMED_REFUNDED,
                source_type="invoice", source_id=invoice.id,
                actor_id=actor_id,
            )
        except LoyaltyError:
            pass  # can't fail — a refund only ever adds
        db.session.flush()

    # Claw back earned points — cap at current balance so a
    # customer who's already spent them elsewhere doesn't block
    # the void (ticket §11 edge case).
    if invoice.loyalty_points_awarded_at is not None and \
            int(invoice.loyalty_points_earned or 0) > 0:
        want_back = int(invoice.loyalty_points_earned)
        available = int(customer.loyalty_points_balance or 0)
        take = min(want_back, available)
        if take > 0:
            note = None
            if take < want_back:
                note = (f"عُكس {take} من {want_back} — الباقي مصروف "
                        f"في فاتورة أخرى")
            try:
                _apply_points_delta(
                    customer, -take, LoyaltyReason.EARNED_REVERSED,
                    source_type="invoice", source_id=invoice.id,
                    actor_id=actor_id, reason_note=note,
                )
            except LoyaltyError:
                pass  # already capped — shouldn't happen
            db.session.flush()


# ─── Manual adjust ──────────────────────────────────────────
def adjust_points_manually(customer, delta, reason_note, actor_id):
    """Owner/admin correction path. Reason is mandatory (per §5)."""
    if not (reason_note or "").strip():
        raise LoyaltyError("سبب التعديل مطلوب")
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        raise LoyaltyError("قيمة التعديل غير صالحة") from None
    if delta == 0:
        raise LoyaltyError("قيمة التعديل يجب أن تكون غير صفر")
    result = _apply_points_delta(
        customer, delta, LoyaltyReason.MANUAL_ADJUSTMENT,
        actor_id=actor_id, reason_note=reason_note.strip(),
    )
    db.session.commit()
    return result
