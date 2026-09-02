"""MARSOUD-PURCHASE-ORDERS-01 (2026-09-02) — إذن الاستلام (GRN).

Immutable receipt-confirmation document. Records "who received what,
when" for a purchase order. **Deliberately posts no journal entry
and moves no stock** — see ticket §2 for the design decision.

No update / delete flow. If the operator misreads a quantity, the
correction path is a downstream inventory ADJUSTMENT (via the
existing inventory ops), not editing this row.
"""
from datetime import datetime, date
from app import db


class GoodsReceiptNote(db.Model):
    __tablename__ = "goods_receipt_notes"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    number = db.Column(db.String(20), index=True, nullable=False)
    purchase_order_id = db.Column(
        db.Integer,
        db.ForeignKey("purchase_orders.id"),
        nullable=False, index=True)
    received_date = db.Column(db.Date, default=date.today,
                               nullable=False)
    received_by_id = db.Column(db.Integer,
                                db.ForeignKey("users.id"),
                                nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)

    company = db.relationship("Company")
    purchase_order = db.relationship(
        "PurchaseOrder",
        backref=db.backref("receipts", lazy="dynamic"))
    received_by = db.relationship(
        "User", foreign_keys=[received_by_id])
    items = db.relationship(
        "GoodsReceiptItem",
        backref="grn",
        cascade="all, delete-orphan")

    __table_args__ = (
        db.UniqueConstraint("company_id", "number",
                             name="uq_grn_number"),
    )


class GoodsReceiptItem(db.Model):
    __tablename__ = "goods_receipt_items"

    id = db.Column(db.Integer, primary_key=True)
    grn_id = db.Column(db.Integer,
                        db.ForeignKey("goods_receipt_notes.id"),
                        nullable=False)
    po_item_id = db.Column(
        db.Integer,
        db.ForeignKey("purchase_order_items.id"),
        nullable=False)
    quantity_received = db.Column(db.Numeric(15, 3),
                                    nullable=False)

    po_item = db.relationship("PurchaseOrderItem",
                                foreign_keys=[po_item_id])
