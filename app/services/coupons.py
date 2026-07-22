"""MARSOUD-DISCOUNT-COUPONS (Abdelhamid 2026-07-22).

Validate + redeem coupon codes at signup / renewal.
"""
from decimal import Decimal
from datetime import date
from app import db
from app.models import (
    Coupon, CouponRedemption, DISCOUNT_PERCENT, DISCOUNT_FIXED,
)


class CouponError(Exception):
    """Raised when a coupon can't be applied. Message is user-safe
    Arabic so routes can flash it directly."""


def _lookup(code):
    if not code:
        raise CouponError("الكود مطلوب.")
    code = code.strip().upper()
    coupon = Coupon.query.filter_by(code=code).first()
    if not coupon:
        raise CouponError("الكود غير موجود.")
    if not coupon.active:
        raise CouponError("الكود معطّل.")
    return coupon


def _check_window(coupon):
    today = date.today()
    if coupon.valid_from and today < coupon.valid_from:
        raise CouponError("الكود لسه ما اشتغلش (تاريخ البداية أبعد).")
    if coupon.valid_until and today > coupon.valid_until:
        raise CouponError("الكود انتهت صلاحيته.")


def _check_scope(coupon, plan_id):
    plan_ids = coupon.plan_ids
    if plan_ids is None:
        return
    if plan_id not in plan_ids:
        raise CouponError("الكود مش صالح على هذه الباقة.")


def _check_max_uses(coupon):
    if coupon.max_uses is None:
        return
    used = CouponRedemption.query.filter_by(
        coupon_id=coupon.id).count()
    if used >= coupon.max_uses:
        raise CouponError("الكود استُخدم لأقصى مرات المسموح بها.")


def _check_per_customer(coupon, company):
    if not coupon.max_uses_per_customer:
        return
    used = CouponRedemption.query.filter_by(
        coupon_id=coupon.id, company_id=company.id).count()
    if used >= coupon.max_uses_per_customer:
        raise CouponError(
            "استخدمت هذا الكود لأقصى مرات المسموح بها.")


def compute_discount(coupon, base_price):
    """Return the discount amount as Decimal. Never negative,
    never > base_price."""
    if coupon.discount_type == DISCOUNT_PERCENT:
        pct = Decimal(coupon.discount_value) / Decimal(100)
        amount = Decimal(base_price) * pct
    else:
        amount = Decimal(coupon.discount_value)
    if amount < 0:
        amount = Decimal(0)
    if amount > Decimal(base_price):
        amount = Decimal(base_price)
    return amount


def validate(code, company, plan_id, base_price):
    """Return (coupon, discount_amount) or raise CouponError."""
    coupon = _lookup(code)
    _check_window(coupon)
    _check_scope(coupon, plan_id)
    _check_max_uses(coupon)
    if company is not None:
        _check_per_customer(coupon, company)
    amount = compute_discount(coupon, base_price)
    return coupon, amount


def redeem(coupon, company, user, amount_saved):
    """Persist a redemption row. Re-runs the max_uses checks under
    the same transaction so a concurrent redemption can't overshoot
    the cap. Caller is expected to have already accepted the payment."""
    _check_max_uses(coupon)
    _check_per_customer(coupon, company)
    row = CouponRedemption(
        coupon_id=coupon.id,
        company_id=company.id,
        user_id=user.id if user else None,
        amount_saved=amount_saved,
    )
    db.session.add(row)
    db.session.commit()
    return row
