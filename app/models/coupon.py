"""MARSOUD-DISCOUNT-COUPONS (Abdelhamid 2026-07-22)."""
import json
from datetime import datetime
from app import db


DISCOUNT_PERCENT = "PERCENT"
DISCOUNT_FIXED = "FIXED"


class Coupon(db.Model):
    __tablename__ = "coupons"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), nullable=False,
                     unique=True, index=True)
    discount_type = db.Column(db.String(10), nullable=False)
    discount_value = db.Column(db.Numeric(15, 2), nullable=False)
    valid_from = db.Column(db.Date)
    valid_until = db.Column(db.Date)
    max_uses = db.Column(db.Integer)
    max_uses_per_customer = db.Column(db.Integer,
                                        default=1, nullable=False)
    applies_to_plan_ids = db.Column(db.Text)   # JSON list
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)

    @property
    def plan_ids(self):
        if not self.applies_to_plan_ids:
            return None
        try:
            data = json.loads(self.applies_to_plan_ids)
            return [int(x) for x in data]
        except (ValueError, TypeError):
            return None

    def set_plan_ids(self, ids):
        if not ids:
            self.applies_to_plan_ids = None
        else:
            self.applies_to_plan_ids = json.dumps(
                [int(i) for i in ids])


class CouponRedemption(db.Model):
    __tablename__ = "coupon_redemptions"

    id = db.Column(db.Integer, primary_key=True)
    coupon_id = db.Column(db.Integer,
                          db.ForeignKey("coupons.id",
                                        ondelete="CASCADE"),
                          nullable=False, index=True)
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    amount_saved = db.Column(db.Numeric(15, 2), nullable=False)
    redeemed_at = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)

    coupon = db.relationship("Coupon", foreign_keys=[coupon_id])
    company = db.relationship("Company", foreign_keys=[company_id])
    user = db.relationship("User", foreign_keys=[user_id])
