#!/usr/bin/env python3
"""MARSOUD-REVERSE-ONCE (2026-08-05) — a journal is reversed at most once.

reverse_journal refused to reverse a REVERSAL (`if original.is_reversal`)
but never asked whether the entry in front of it had ALREADY been
reversed. Reverse the same entry twice and you get two reversing
entries: the account lands at -1x the original instead of zero. Nothing
downstream complains, because both reversing entries balance perfectly.

Two things made it reachable rather than theoretical:

  · the journals route offers the «عكس» button on an entry that is
    already reversed, so a double click is enough
  · the second pass re-enters _undo_source_side_effects, rolling the
    source row back twice — an open item cancelled again, an accrual
    un-settled again

The guard uses JournalEntry.reversal_of, which already existed. No
migration.

Checks
  1. the first reversal still works, and lands the account at zero
  2. a second reversal is refused, with a message naming the existing one
  3. the refusal writes NOTHING — no entry, no line, no balance move
  4. the refusal does not touch the source row either
  5. a PAUSED reversal does not block a new one (it has no ledger effect)
  6. reversing a reversal is still refused (the original guard)
  7. reversing a missing entry is still a clean error
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__REVONCE_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _setup():
    _teardown()
    from app.models import Company, User
    from app.services.seed_coa import seed_default_coa
    co = Company(name=f"{PREFIX}CO__", base_currency="EGP", vat_rate=0)
    db.session.add(co)
    db.session.flush()
    seed_default_coa(co.id)
    db.session.commit()
    u = User.query.first()
    _STATE.update(cid=co.id, uid=u.id if u else None)


def _teardown():
    from app.models import Company
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        db.session.execute(text(
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id=:c)"), {"c": cid})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text("DELETE FROM companies WHERE id=:c"),
                           {"c": cid})
        db.session.commit()


def _acc(code):
    from app.services.ledger import get_account_by_code
    return get_account_by_code(_STATE["cid"], code)


def _balance(code):
    """Net debit-minus-credit on an account, active entries only."""
    from app.models import JournalEntry, JournalLine
    a = _acc(code)
    rows = (db.session.query(JournalLine)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .filter(JournalLine.account_id == a.id,
                    JournalEntry.is_active.is_(True)).all())
    return round(sum(float(r.debit_base) - float(r.credit_base)
                     for r in rows), 2)


def _entry_count():
    from app.models import JournalEntry
    return JournalEntry.query.filter_by(company_id=_STATE["cid"]).count()


def _post(amount=1000, **kw):
    """A plain two-line entry: Dr 1110 cash, Cr 3100 capital."""
    from app.services.ledger import post_journal
    return post_journal(
        company_id=_STATE["cid"], description="revonce probe",
        lines=[{"account_id": _acc("1110").id, "debit": amount, "credit": 0},
               {"account_id": _acc("3100").id, "debit": 0, "credit": amount}],
        created_by=_STATE["uid"], **kw)


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. the first reversal still works and nets the account to zero")
def _():
    from app.services.ledger import reverse_journal
    before = _balance("1110")
    e = _post(1000)
    assert _balance("1110") == before + 1000, "the probe entry did not post"
    rev = reverse_journal(e.id, created_by=_STATE["uid"])
    after = _balance("1110")
    assert after == before, (
        f"after one reversal 1110 is {after}, expected {before}")
    assert rev.is_reversal and rev.reversal_of == e.id
    _STATE["entry_id"] = e.id
    _STATE["rev_id"] = rev.id
    return f"1110 {before} -> {before + 1000} -> {after}, reversal {rev.number}"


@check("2. THE BUG: a second reversal of the same entry is refused")
def _():
    """Measured before the guard: the second reversal posted, and 1110
    ended at -1000 instead of 0."""
    from app.services.ledger import reverse_journal, LedgerError
    try:
        reverse_journal(_STATE["entry_id"], created_by=_STATE["uid"])
        raise AssertionError(
            "the same entry was reversed TWICE — the account is now at "
            "-1x the original and both entries balance, so nothing else "
            "will ever notice")
    except LedgerError as e:
        msg = str(e)
    from app.models import JournalEntry
    existing = db.session.get(JournalEntry, _STATE["rev_id"])
    assert str(existing.number or existing.id) in msg, (
        f"the message does not name the existing reversal: {msg}")
    return msg


@check("3. the refused reversal wrote nothing at all")
def _():
    from app.services.ledger import reverse_journal, LedgerError
    from app.models import JournalLine
    before_entries = _entry_count()
    before_lines = JournalLine.query.count()
    before_bal = _balance("1110")
    for _attempt in range(3):
        try:
            reverse_journal(_STATE["entry_id"], created_by=_STATE["uid"])
        except LedgerError:
            pass
    db.session.commit()          # anything pending would land here
    assert _entry_count() == before_entries, (
        f"entries {before_entries} -> {_entry_count()}")
    assert JournalLine.query.count() == before_lines, "lines were written"
    assert _balance("1110") == before_bal, (
        f"1110 moved {_balance('1110') - before_bal} on a refused reversal")
    return f"3 more attempts: {before_entries} entries, 1110 still {before_bal}"


@check("4. …and it does not roll the source row back a second time")
def _():
    """The real damage. The second pass re-entered
    _undo_source_side_effects, so an open item was cancelled twice — and
    for a settlement it would have credited the amount back twice."""
    from app.models import OpenItem, JournalEntry
    from app.services.ledger import reverse_journal, LedgerError, postable_under
    from app.services.accounting_ops import get_operation, run_operation
    from datetime import date
    cid = _STATE["cid"]
    exp = postable_under(cid, "5000")[0]
    entry = run_operation(
        get_operation("accrue-expense"), cid,
        {"amount": "300", "date": date.today().isoformat(),
         "expense_account_id": str(exp.id)}, actor_id=_STATE["uid"])
    item_id = db.session.get(JournalEntry, entry.id).source_id
    reverse_journal(entry.id, created_by=_STATE["uid"])
    item = db.session.get(OpenItem, item_id)
    db.session.refresh(item)
    assert item.status.value == "CANCELLED"
    first_closed_at = item.closed_at
    first_reversal = item.reversal_entry_id

    try:
        reverse_journal(entry.id, created_by=_STATE["uid"])
        raise AssertionError("the accrual journal was reversed twice")
    except LedgerError:
        pass
    db.session.refresh(item)
    assert item.closed_at == first_closed_at, (
        "the item was closed a second time — closed_at moved")
    assert item.reversal_entry_id == first_reversal, (
        "the item was re-pointed at a second reversal entry")
    return "open item cancelled exactly once"


@check("5. a reversal cannot be paused, so the guard's is_active branch "
       "is unreachable today")
def _():
    """Written after check 5 originally assumed you could pause a reversal
    and found you cannot. pause_entry (services/journals.py:15) refuses
    them outright, and it is the ONLY thing that sets is_active=False on
    an entry — so every reversal the guard sees is active.

    The filter stays anyway: matching any reversal, active or not, would
    make an entry permanently un-reversible if a bad reversal were ever
    deactivated by a data fix. This check pins both halves — that the
    branch is currently unreachable, and that it behaves correctly if it
    ever stops being."""
    from app.services.ledger import reverse_journal, LedgerError
    from app.services.journals import pause_entry
    from app.models import JournalEntry

    before = _balance("1110")
    e = _post(700)
    rev = reverse_journal(e.id, created_by=_STATE["uid"])
    assert _balance("1110") == before, "reversal did not neutralise"

    # (a) the UI cannot get there
    try:
        pause_entry(db.session.get(JournalEntry, rev.id), "audit",
                    _STATE["uid"])
        raise AssertionError(
            "a reversal can now be paused — the guard's is_active branch "
            "is reachable, so re-reversal behaviour is now user-visible "
            "and this check should be rewritten to exercise it properly")
    except LedgerError as err:
        refused = str(err)

    # (b) but if it ever is deactivated, re-reversal must work
    row = db.session.get(JournalEntry, rev.id)
    row.is_active = False
    db.session.commit()
    assert _balance("1110") == before + 700, (
        "deactivating the reversal did not restore the original's effect")

    rev2 = reverse_journal(e.id, created_by=_STATE["uid"])
    assert rev2.id != rev.id, "no new reversal was created"
    assert _balance("1110") == before, (
        "the replacement reversal did not neutralise the entry")
    return f"pause refused ({refused}); deactivated -> re-reversed OK"


@check("6. reversing a reversal is still refused (the original guard)")
def _():
    from app.services.ledger import reverse_journal, LedgerError
    try:
        reverse_journal(_STATE["rev_id"], created_by=_STATE["uid"])
        raise AssertionError("a reversal entry was itself reversed")
    except LedgerError as e:
        return str(e)


@check("7. reversing a missing entry is still a clean error")
def _():
    from app.services.ledger import reverse_journal, LedgerError
    try:
        reverse_journal(99999999, created_by=_STATE["uid"])
        raise AssertionError("a non-existent entry was reversed")
    except LedgerError as e:
        return str(e)


@check("8. reversing a SETTLEMENT twice does not double-reopen the item")
def _():
    """MARSOUD-DOUBLE-REVERSAL-EXPAND (2026-08-06) — the specific
    scenario the follow-up ticket calls out. Check 4 already pins the
    accrual path (reversing the entry that CREATES an open item does
    not double-cancel it); this pins the settlement path (reversing
    the entry that CLOSES a settlement does not double-reopen the
    item).

    The MARSOUD-REVERSE-ONCE guard covers both — but only check 4
    proved it for the source_type=open_item branch of
    _undo_source_side_effects. This proves it for the source_type=
    open_item_settle branch (which calls reverse_settlement, which
    reopens the item and rewrites settled_amount/status/closed_at)."""
    from app.models import (OpenItem, OpenItemSettlement, OpenItemStatus,
                            JournalEntry)
    from app.services.ledger import reverse_journal, LedgerError, postable_under
    from app.services.accounting_ops import get_operation, run_operation
    from datetime import date

    cid = _STATE["cid"]
    exp = postable_under(cid, "5000")[0]
    money = postable_under(cid, "1110")[0]

    # 1. Accrue an obligation for 1000.
    accrual_entry = run_operation(
        get_operation("accrue-expense"), cid,
        {"amount": "1000", "date": date.today().isoformat(),
         "expense_account_id": str(exp.id)}, actor_id=_STATE["uid"])
    item_id = db.session.get(JournalEntry, accrual_entry.id).source_id

    # 2. Settle it in full. This produces the open_item_settle
    #    journal the ticket wants us to double-reverse.
    settle_entry = run_operation(
        get_operation("settle-accrued-expense"), cid,
        {"amount": "1000", "date": date.today().isoformat(),
         "open_item_id": str(item_id),
         "account_id": str(money.id)}, actor_id=_STATE["uid"])
    leg_id = db.session.get(JournalEntry, settle_entry.id).source_id

    item = db.session.get(OpenItem, item_id)
    db.session.refresh(item)
    assert item.status == OpenItemStatus.SETTLED, (
        f"prep: item did not settle, status={item.status}")

    # 3. First reversal of the settlement — must reopen the item.
    reverse_journal(settle_entry.id, created_by=_STATE["uid"])
    db.session.refresh(item)
    leg = db.session.get(OpenItemSettlement, leg_id)
    db.session.refresh(leg)
    assert item.status in (OpenItemStatus.OPEN,
                            OpenItemStatus.PARTIALLY_SETTLED), (
        f"after first reversal item.status={item.status}")
    assert leg.reversed_at is not None, (
        "first reversal did not stamp reversed_at on the settlement leg")

    # 4. Snapshot every mutable field on the item + leg BEFORE the
    #    second (refused) attempt.
    snap_status = item.status
    snap_settled_amount = item.settled_amount
    snap_closed_at = item.closed_at
    snap_leg_reversed_at = leg.reversed_at

    # 5. Second reversal — MUST refuse before any effect.
    try:
        reverse_journal(settle_entry.id, created_by=_STATE["uid"])
        raise AssertionError(
            "the settlement journal was reversed TWICE — the item "
            "state was rewritten a second time")
    except LedgerError:
        pass

    # 6. Nothing on the item or leg moved. Byte-for-byte identical.
    db.session.refresh(item)
    db.session.refresh(leg)
    assert item.status == snap_status, (
        f"item status moved after refused reversal: "
        f"{snap_status} -> {item.status}")
    assert item.settled_amount == snap_settled_amount, (
        f"settled_amount moved after refused reversal: "
        f"{snap_settled_amount} -> {item.settled_amount}")
    assert item.closed_at == snap_closed_at, (
        f"closed_at moved after refused reversal: "
        f"{snap_closed_at} -> {item.closed_at}")
    assert leg.reversed_at == snap_leg_reversed_at, (
        f"leg reversed_at moved after refused reversal: "
        f"{snap_leg_reversed_at} -> {leg.reversed_at}")
    return "settlement reversed exactly once; item + leg unchanged"


def main():
    app = create_app()
    _STATE["app"] = app
    passed = failed = 0
    with app.app_context():
        _setup()
        try:
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(fixture company cleaned up)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
