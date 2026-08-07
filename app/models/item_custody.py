"""MARSOUD-ITEM-CUSTODY-01 (2026-08-07) — item (physical) custody
models.

Companion to `cash_custody.py`. Cash custody tracks money handed
to a holder; item custody tracks WHO physically holds a laptop,
phone, SIM, uniform, tool, or company car — either a specific
`FixedAsset` on the books OR a non-capitalised item that was
expensed at purchase.

Three tables + one status enum:

  CustodyItem            the "what" — item registry (fixed-asset
                         linked OR standalone)
  ItemCustodyRequest     request → PENDING → APPROVED / REJECTED
  ItemCustody            live custody → ACTIVE → RETURNED_GOOD /
                         RETURNED_DAMAGED / LOST / TRANSFERRED

Holder polymorphism reuses `CustodyHolderType` + the exact same
`_HOLDER_CHECK` SQL from cash_custody so the "exactly one holder"
guarantee is enforced at the DB level identically on both.

Every state change flows through `app/services/item_custody.py` —
direct model mutation bypasses the invariants (one-active-per-item,
disposal_pending_at set on LOST/DAMAGED for fixed-asset-linked,
journal posted only when appropriate) and will silently corrupt.
"""
import enum
from datetime import datetime
from app import db

# Re-use the cash-custody enum + CHECK — the ticket explicitly asks
# for one custody module with two flavours, so the holder rules
# have to be identical.
from app.models.cash_custody import CustodyHolderType, _HOLDER_CHECK


# ─── Enums ─────────────────────────────────────────────────────
class ItemCustodyRequestStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ItemCustodyStatus(enum.Enum):
    """The five outcomes item-custody covers. RETURNED_GOOD and
    TRANSFERRED never post a journal; RETURNED_DAMAGED / LOST may
    (see service). ACTIVE is the only "open" state — the invariant
    "one active per item at a time" is enforced there."""
    ACTIVE = "ACTIVE"
    RETURNED_GOOD = "RETURNED_GOOD"
    RETURNED_DAMAGED = "RETURNED_DAMAGED"
    LOST = "LOST"
    TRANSFERRED = "TRANSFERRED"


# ─── CustodyItem ───────────────────────────────────────────────
class CustodyItem(db.Model):
    """The item register. Row per physical thing the company hands
    to staff. `fixed_asset_id` is the bridge to the ledger when the
    item is a capitalised asset; `estimated_value` is the manual
    valuation when it isn't (used only for reporting + damage-charge
    ceilings, never as a ledger balance).

    Business rule enforced in the service (not CHECK): exactly one
    of `fixed_asset_id` or `estimated_value` should be meaningful.
    A CHECK would refuse imported legacy rows where both happen to
    be null (an item registered before its value was known); the
    service refuses the double-set on new-item creation, which is
    the actual failure mode we care about."""
    __tablename__ = "custody_items"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    serial_number = db.Column(db.String(100), nullable=True)
    category = db.Column(db.String(60), nullable=True)
    # The ledger bridge. When set, this CustodyItem IS the given
    # FixedAsset (typically 1:1). Nullable — non-capitalised items
    # like uniforms don't have a fixed-asset counterpart.
    fixed_asset_id = db.Column(
        db.Integer, db.ForeignKey("fixed_assets.id", ondelete="SET NULL"),
        nullable=True, index=True)
    # Manual valuation for standalone items. Ignored when
    # fixed_asset_id is set (asset carries cost/NBV).
    estimated_value = db.Column(db.Numeric(15, 2), nullable=True)
    # Soft retirement — set False when the item is disposed
    # (fixed-asset path via complete_disposal_for_custody), lost
    # unrecoverably, or manually retired by an accountant.
    is_active = db.Column(db.Boolean, default=True, nullable=False,
                           index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    company = db.relationship("Company")
    fixed_asset = db.relationship("FixedAsset",
                                    foreign_keys=[fixed_asset_id])
    creator = db.relationship("User", foreign_keys=[created_by])

    def __repr__(self):                                  # pragma: no cover
        tag = f"asset={self.fixed_asset_id}" if self.fixed_asset_id else "standalone"
        return f"<CustodyItem {self.id} '{self.name}' ({tag})>"


# ─── ItemCustodyRequest ────────────────────────────────────────
class ItemCustodyRequest(db.Model):
    """Request to take an item into custody. Same holder polymorphism
    as CashCustodyRequest — the CHECK constraint at the DB level
    guarantees exactly one of employee_id / department_id is set."""
    __tablename__ = "item_custody_requests"
    __table_args__ = (
        db.CheckConstraint(_HOLDER_CHECK,
                            name="ck_item_custody_request_one_holder"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    item_id = db.Column(db.Integer,
                         db.ForeignKey("custody_items.id",
                                       ondelete="CASCADE"),
                         nullable=False, index=True)

    holder_type = db.Column(db.Enum(CustodyHolderType),
                             nullable=False, index=True)
    employee_id = db.Column(db.Integer,
                             db.ForeignKey("employees.id",
                                            ondelete="SET NULL"),
                             nullable=True, index=True)
    department_id = db.Column(db.Integer,
                               db.ForeignKey("departments.id",
                                              ondelete="SET NULL"),
                               nullable=True, index=True)

    purpose = db.Column(db.Text, nullable=False)
    status = db.Column(db.Enum(ItemCustodyRequestStatus),
                        default=ItemCustodyRequestStatus.PENDING,
                        nullable=False, index=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    review_note = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))

    company = db.relationship("Company")
    item = db.relationship("CustodyItem",
                             backref=db.backref("requests", lazy="dynamic"))
    employee = db.relationship("Employee", foreign_keys=[employee_id])
    department = db.relationship("Department",
                                   foreign_keys=[department_id])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    creator = db.relationship("User", foreign_keys=[created_by])

    @property
    def holder(self):
        return self.employee or self.department

    @property
    def holder_name(self):
        h = self.holder
        return h.name if h else "—"


# ─── ItemCustody ───────────────────────────────────────────────
class ItemCustody(db.Model):
    """The live custody row. Same holder shape as cash-custody +
    item bridge + settlement bookkeeping. Invariants:

      · exactly one holder (DB CHECK)
      · for a given item_id, at most one row with status=ACTIVE
        (enforced by service — Python-side check inside
        hand_over_item + approve_item_request race guard)
      · disposal_pending_at is set iff RETURNED_DAMAGED / LOST on
        a fixed-asset-linked item AND completion hasn't happened yet
      · transferred_to_custody_id is set iff status=TRANSFERRED"""
    __tablename__ = "item_custodies"
    __table_args__ = (
        db.CheckConstraint(_HOLDER_CHECK,
                            name="ck_item_custody_one_holder"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    item_id = db.Column(db.Integer,
                         db.ForeignKey("custody_items.id",
                                       ondelete="CASCADE"),
                         nullable=False, index=True)
    request_id = db.Column(db.Integer,
                            db.ForeignKey("item_custody_requests.id",
                                          ondelete="SET NULL"),
                            nullable=True)

    holder_type = db.Column(db.Enum(CustodyHolderType),
                             nullable=False, index=True)
    employee_id = db.Column(db.Integer,
                             db.ForeignKey("employees.id",
                                            ondelete="SET NULL"),
                             nullable=True, index=True)
    department_id = db.Column(db.Integer,
                               db.ForeignKey("departments.id",
                                              ondelete="SET NULL"),
                               nullable=True, index=True)

    handed_over_on = db.Column(db.Date, nullable=False)
    condition_at_handover = db.Column(db.Text, nullable=True)

    status = db.Column(db.Enum(ItemCustodyStatus),
                        default=ItemCustodyStatus.ACTIVE,
                        nullable=False, index=True)
    settled_on = db.Column(db.Date, nullable=True)
    settled_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    settlement_note = db.Column(db.Text, nullable=True)
    condition_at_return = db.Column(db.Text, nullable=True)

    # Damage assessment — set by the accountant at settlement for
    # RETURNED_DAMAGED / LOST. Always in nominal currency; the
    # standalone-item path posts this as the journal amount.
    damage_value = db.Column(db.Numeric(15, 2),
                              default=0, nullable=False)
    # When True + LOST/DAMAGED, the amount hits the employee's 2130
    # sub-account (a receivable on the employee). Only valid when
    # holder_type=EMPLOYEE — service refuses on department holder.
    charged_to_employee = db.Column(db.Boolean, default=False,
                                     nullable=False)

    # Populated only when the settlement actually posts a journal
    # (standalone item + charged_to_employee=True). Stays NULL for
    # RETURNED_GOOD / TRANSFERRED / uncharged standalone / awaiting-
    # disposal on fixed-asset-linked.
    journal_entry_id = db.Column(db.Integer,
                                  db.ForeignKey("journal_entries.id",
                                                ondelete="SET NULL"),
                                  nullable=True)

    # ─── Fixed-asset-linked disposal path ────────────────────
    # Set when the settlement outcome is LOST or RETURNED_DAMAGED
    # AND the item has a fixed_asset_id. The accountant must then
    # invoke complete_disposal_for_custody, which calls
    # dispose_asset() and stamps disposal_asset_result_id.
    disposal_pending_at = db.Column(db.DateTime, nullable=True,
                                     index=True)
    disposal_asset_result_id = db.Column(
        db.Integer, db.ForeignKey("fixed_assets.id",
                                    ondelete="SET NULL"),
        nullable=True)

    # ─── TRANSFERRED chain link ─────────────────────────────
    # When status=TRANSFERRED, points at the new ACTIVE custody row
    # that took over. Populated in the same transaction as the
    # settle → new-row creation so there's no gap in coverage.
    transferred_to_custody_id = db.Column(
        db.Integer, db.ForeignKey("item_custodies.id",
                                    ondelete="SET NULL"),
        nullable=True)

    # Cron dedup: one-shot bell to accountant when custody has been
    # ACTIVE longer than the configured threshold. Cleared on any
    # settlement.
    overdue_notified_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancelled_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    cancelled_at = db.Column(db.DateTime)
    cancel_reason = db.Column(db.Text)

    company = db.relationship("Company")
    item = db.relationship("CustodyItem",
                             backref=db.backref("custodies", lazy="dynamic"))
    request = db.relationship("ItemCustodyRequest",
                                foreign_keys=[request_id],
                                backref=db.backref("custody",
                                                    uselist=False))
    employee = db.relationship("Employee", foreign_keys=[employee_id])
    department = db.relationship("Department",
                                   foreign_keys=[department_id])
    entry = db.relationship("JournalEntry",
                              foreign_keys=[journal_entry_id])
    disposal_asset = db.relationship(
        "FixedAsset", foreign_keys=[disposal_asset_result_id])
    transferred_to = db.relationship(
        "ItemCustody", foreign_keys=[transferred_to_custody_id],
        remote_side=[id])
    settler = db.relationship("User", foreign_keys=[settled_by])
    creator = db.relationship("User", foreign_keys=[created_by])
    canceller = db.relationship("User", foreign_keys=[cancelled_by])

    @property
    def holder(self):
        return self.employee or self.department

    @property
    def holder_name(self):
        h = self.holder
        return h.name if h else "—"

    @property
    def is_open(self):
        return self.status == ItemCustodyStatus.ACTIVE

    @property
    def is_awaiting_disposal(self):
        return self.disposal_pending_at is not None

    def __repr__(self):                                  # pragma: no cover
        return (f"<ItemCustody item={self.item_id} "
                f"holder={self.holder_type.value}/"
                f"{self.employee_id or self.department_id} "
                f"status={self.status.value}>")
