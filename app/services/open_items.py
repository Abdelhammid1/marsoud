"""MARSOUD-OPS-FOUNDATION (2026-08-05) — creating and settling open items.

Every two-sided operation goes through here. The point is that "how much
is still owed" is arithmetic in ONE place, so a settlement wizard cannot
be a free amount box — the ticket is explicit that a payment must be tied
to the thing it settles, or you can pay the same accrual twice and nobody
finds out.

Three rules live here and nowhere else:
  · you settle an ITEM, never a bare amount
  · you cannot settle more than the remainder
  · you cannot settle something already closed

Reversal is the fourth: the creating journal carries source_id = the
item's id, so ledger._undo_source_side_effects can reopen it. A settled
item whose journal was reversed must not stay settled.
"""
from datetime import date, datetime

from app import db
from app.models import (
    OpenItem, OpenItemSettlement, OpenItemStatus, SETTLEABLE_STATUSES,
)


class OpenItemError(Exception):
    """User-facing problem settling or creating an item."""


# Journal source_types written by open-item operations. Registered in
# app/services/source_reference.py so the ledger labels them in Arabic.
SOURCE_CREATE = "open_item"
SOURCE_SETTLE = "open_item_settle"


def create_open_item(company_id, kind, account_id, amount, *,
                     description=None, party_type=None, party_id=None,
                     due_date=None, created_by=None, note=None):
    """Record an amount that will be discharged later. Flushes, no commit —
    the caller posts the journal and owns the transaction."""
    amount = round(float(amount or 0), 2)
    if amount <= 0:
        raise OpenItemError("المبلغ يجب أن يكون أكبر من صفر")
    item = OpenItem(
        company_id=company_id, kind=kind, account_id=account_id,
        description=description, party_type=party_type, party_id=party_id,
        original_amount=amount, settled_amount=0,
        status=OpenItemStatus.OPEN, due_date=due_date,
        created_by=created_by, note=note,
    )
    db.session.add(item)
    db.session.flush()
    return item


def open_items_for(company_id, kind=None):
    """The items still needing settlement, newest last."""
    q = OpenItem.query.filter(
        OpenItem.company_id == company_id,
        OpenItem.status.in_(SETTLEABLE_STATUSES),
    )
    if kind:
        q = q.filter(OpenItem.kind == kind)
    # `remaining` is a Python property, so the > 0 test cannot be pushed
    # into SQL; these lists are short.
    return [i for i in q.order_by(OpenItem.id).all() if i.remaining > 0.005]


def open_item_choices(company_id, kind=None):
    """[(group_label, [(id, label), ...]), ...] for the picker.

    The label carries the remainder, because "which of these do I mean" is
    unanswerable from a description alone once two accruals have the same
    name.
    """
    items = open_items_for(company_id, kind=kind)
    if not items:
        return []
    return [("بنود مفتوحة", [
        (i.id, f"{i.description or i.kind} — متبقٍ {i.remaining:,.2f}")
        for i in items
    ])]


def resolve_open_item(company_id, item_id, kind=None):
    """Validate a submitted id against what the picker would have offered."""
    allowed = {i.id: i for i in open_items_for(company_id, kind=kind)}
    if not allowed:
        raise OpenItemError("لا توجد بنود مفتوحة تحتاج سداد")
    try:
        iid = int(item_id or 0)
    except (TypeError, ValueError):
        iid = 0
    if not iid:
        raise OpenItemError("اختر البند المراد سداده")
    item = allowed.get(iid)
    if item is None:
        # Either it belongs to another company, or it is already closed.
        # Both answer the same way: it is not something you may settle.
        raise OpenItemError("البند المختار غير متاح للسداد")
    return item


def settle_open_item(item, amount, *, journal_entry_id=None,
                     settled_on=None, created_by=None):
    """Pay an item down by `amount`. Flushes, no commit.

    Refuses the two mistakes a free-amount box makes possible: paying a
    closed item, and paying more than is left.
    """
    amount = round(float(amount or 0), 2)
    if amount <= 0:
        raise OpenItemError("المبلغ يجب أن يكون أكبر من صفر")
    if item.status not in SETTLEABLE_STATUSES:
        raise OpenItemError(
            f"هذا البند {_status_ar(item.status)} — لا يمكن سداده مرة أخرى")
    remaining = item.remaining
    if remaining <= 0.005:
        raise OpenItemError("هذا البند مسدَّد بالكامل")
    if amount > remaining + 0.005:
        raise OpenItemError(
            f"المبلغ ({amount:,.2f}) أكبر من المتبقي ({remaining:,.2f})")

    leg = OpenItemSettlement(
        company_id=item.company_id, open_item_id=item.id, amount=amount,
        settled_on=settled_on or date.today(),
        journal_entry_id=journal_entry_id, created_by=created_by,
    )
    db.session.add(leg)

    item.settled_amount = round(float(item.settled_amount or 0) + amount, 2)
    if item.remaining <= 0.005:
        item.settled_amount = item.original_amount
        item.status = OpenItemStatus.SETTLED
        item.closed_at = datetime.utcnow()
    else:
        item.status = OpenItemStatus.PARTIALLY_SETTLED
    db.session.flush()
    return leg


def cancel_open_item(item, reversal_entry_id=None):
    """The creating journal was reversed — the item never really existed."""
    item.status = OpenItemStatus.CANCELLED
    item.closed_at = datetime.utcnow()
    if reversal_entry_id:
        item.reversal_entry_id = reversal_entry_id


def reverse_settlement(settlement):
    """Undo one settlement leg and reopen its item.

    The leg is kept and stamped `reversed_at` rather than deleted: the
    money did move once, and deleting the row would erase that.
    """
    if settlement.reversed_at is not None:
        return
    item = settlement.item
    settlement.reversed_at = datetime.utcnow()
    item.settled_amount = round(
        float(item.settled_amount or 0) - float(settlement.amount or 0), 2)
    if item.settled_amount < 0:
        item.settled_amount = 0
    # Reopen: a settled item whose journal was reversed must not stay
    # settled, which is exactly the state the ticket calls out.
    if item.status in (OpenItemStatus.SETTLED,
                       OpenItemStatus.PARTIALLY_SETTLED):
        item.status = (OpenItemStatus.PARTIALLY_SETTLED
                       if float(item.settled_amount or 0) > 0.005
                       else OpenItemStatus.OPEN)
        item.closed_at = None


_STATUS_AR = {
    OpenItemStatus.OPEN: "مفتوح",
    OpenItemStatus.PARTIALLY_SETTLED: "مسدَّد جزئيًا",
    OpenItemStatus.SETTLED: "مسدَّد بالكامل",
    OpenItemStatus.CANCELLED: "ملغي",
    OpenItemStatus.WRITTEN_OFF: "مشطوب",
}


def _status_ar(status):
    return _STATUS_AR.get(status, str(status))
