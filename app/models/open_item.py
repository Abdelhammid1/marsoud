"""MARSOUD-OPS-FOUNDATION (2026-08-05) — the generalised open item.

An amount created by one operation and discharged by another: an accrued
expense and its payment, a prepayment and its consumption, a declared
dividend and its disbursement, a loan and its instalments.

THIS IS A GENERALISATION, NOT AN INVENTION. The same shape already exists
four times in this codebase, each written separately:

  · EmployeeAdvance   amount / remaining / status, closes at zero
  · EmployeeAccrual   amount / paid_amount / settled_at + settling JE
  · CustomerDeposit   amount / status / applied-to document
  · InvoiceInstallment amount / status / paying Payment

Two lessons taken from them:

1. `EmployeeAccrual` keeps ONE `settlement_journal_entry_id` that each
   partial payment overwrites, so the history of who paid what and when
   is lost — its own docstring admits the trail lives only in the
   journals. Settlements here are CHILD ROWS, one per leg.

2. `CustomerDeposit.apply_to_invoice` marks the whole deposit APPLIED even
   when only part of it was used, silently losing the remainder. Nothing
   here closes an item that still has a remainder; only the arithmetic
   decides.

Reversal: the creating journal carries source_id = this row's id, so
`_undo_source_side_effects` in services/ledger.py can find it. A settled
item whose journal was reversed must not stay settled.
"""
import enum
from datetime import datetime

from app import db


class OpenItemStatus(str, enum.Enum):
    OPEN = "OPEN"                          # nothing settled yet
    PARTIALLY_SETTLED = "PARTIALLY_SETTLED"
    SETTLED = "SETTLED"                    # remainder reached zero
    CANCELLED = "CANCELLED"                # creating journal was reversed
    WRITTEN_OFF = "WRITTEN_OFF"            # forgiven, not collected


# Open, in the sense of "still needs settling". The picker offers exactly
# these, which is what stops a payment being recorded against something
# already closed.
SETTLEABLE_STATUSES = (OpenItemStatus.OPEN, OpenItemStatus.PARTIALLY_SETTLED)


class OpenItem(db.Model):
    __tablename__ = "open_items"

    id = db.Column(db.Integer, primary_key=True)
    # ondelete matches migration j0s3o6u9n4p5. Without it here, a DB
    # built by create_all() (fresh dev, some test fixtures) would keep
    # orphan items after a company is deleted while a migrated DB would
    # not — the two would diverge silently.
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id", ondelete="CASCADE"),
                            nullable=False, index=True)

    # Which operation family created it — "accrued_expense", "prepaid",
    # "dividend_declared", "loan"… Kept as a plain string so a new
    # operation needs no migration.
    kind = db.Column(db.String(40), nullable=False, index=True)
    description = db.Column(db.String(255))

    # The account this item sits on: the payable/receivable leg that a
    # settlement will clear. Always postable.
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"),
                            nullable=False)

    # Optional counterparty, when the item belongs to someone.
    party_type = db.Column(db.String(20))          # customer/vendor/employee
    party_id = db.Column(db.Integer)

    original_amount = db.Column(db.Numeric(15, 2), nullable=False)
    settled_amount = db.Column(db.Numeric(15, 2), nullable=False, default=0)

    status = db.Column(db.Enum(OpenItemStatus), nullable=False,
                        default=OpenItemStatus.OPEN, index=True)
    due_date = db.Column(db.Date)

    # The journal that created it. Its source_id points back here, which
    # is how reversal finds this row.
    journal_entry_id = db.Column(db.Integer,
                                  db.ForeignKey("journal_entries.id"))
    reversal_entry_id = db.Column(db.Integer,
                                   db.ForeignKey("journal_entries.id"))

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime)
    note = db.Column(db.Text)

    account = db.relationship("Account", foreign_keys=[account_id])
    settlements = db.relationship(
        "OpenItemSettlement", back_populates="item",
        order_by="OpenItemSettlement.id.asc()",
        cascade="all, delete-orphan",
    )

    @property
    def remaining(self):
        return round(float(self.original_amount or 0)
                     - float(self.settled_amount or 0), 2)

    @property
    def is_settleable(self):
        return self.status in SETTLEABLE_STATUSES and self.remaining > 0.005

    def __repr__(self):                                  # pragma: no cover
        return (f"<OpenItem {self.id} {self.kind} "
                f"{self.remaining}/{self.original_amount} {self.status}>")


class OpenItemSettlement(db.Model):
    """One leg of paying an item down.

    A row per leg, deliberately: EmployeeAccrual's single
    settlement_journal_entry_id is overwritten by each partial payment and
    the earlier ones become untraceable.
    """
    __tablename__ = "open_item_settlements"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    open_item_id = db.Column(db.Integer,
                              db.ForeignKey("open_items.id",
                                            ondelete="CASCADE"),
                              nullable=False, index=True)

    amount = db.Column(db.Numeric(15, 2), nullable=False)
    settled_on = db.Column(db.Date)
    journal_entry_id = db.Column(db.Integer,
                                  db.ForeignKey("journal_entries.id"))
    # Set when this leg is undone, so the row stays as history instead of
    # being deleted.
    reversed_at = db.Column(db.DateTime)

    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    item = db.relationship("OpenItem", back_populates="settlements")

    def __repr__(self):                                  # pragma: no cover
        return f"<OpenItemSettlement {self.id} {self.amount}>"
