"""MARSOUD-LOYALTY-POINTS-01 (2026-09-02) — loyalty points model.

Same discipline as StockMovement: an immutable ledger of every
delta on a customer's balance, with `balance_after` on every row so
you can prove any historical balance without a running SUM(). The
`Customer.loyalty_points_balance` column is a cache; this table is
the truth.
"""
import enum
from datetime import datetime
from app import db


class LoyaltyReason(enum.Enum):
    EARNED = "EARNED"                        # invoice fully PAID for the first time
    REDEEMED = "REDEEMED"                    # spent as a FIXED discount
    EARNED_REVERSED = "EARNED_REVERSED"      # invoice voided → claw back the earn
    REDEEMED_REFUNDED = "REDEEMED_REFUNDED"  # invoice voided → return the redeemed points
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"  # owner/admin manual correction

    @property
    def label_ar(self):
        return {
            "EARNED": "كسب نقاط",
            "REDEEMED": "صرف نقاط",
            "EARNED_REVERSED": "عكس كسب (إلغاء فاتورة)",
            "REDEEMED_REFUNDED": "استرجاع نقاط (إلغاء فاتورة)",
            "MANUAL_ADJUSTMENT": "تعديل يدوي",
        }[self.value]


class LoyaltyPointTransaction(db.Model):
    """Immutable ledger row. No update, no delete."""
    __tablename__ = "loyalty_point_transactions"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    customer_id = db.Column(db.Integer,
                             db.ForeignKey("customers.id"),
                             nullable=False, index=True)
    points_delta = db.Column(db.Integer, nullable=False)  # +/-
    reason = db.Column(db.Enum(LoyaltyReason), nullable=False)
    source_type = db.Column(db.String(30))       # "invoice"
    source_id = db.Column(db.Integer)            # invoice.id
    balance_after = db.Column(db.Integer, nullable=False)
    reason_note = db.Column(db.Text, nullable=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)

    company = db.relationship("Company")
    customer = db.relationship(
        "Customer",
        backref=db.backref(
            "loyalty_transactions", lazy="dynamic",
            order_by="LoyaltyPointTransaction.created_at.desc()"))
    actor = db.relationship("User", foreign_keys=[actor_id])
