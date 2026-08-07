#!/usr/bin/env python3
"""MARSOUD-CRON-VBILL-NO-AUTOPAY-01 (2026-08-07) — production data fix.

Reverses the auto-payment that leaked out of the 2026-08-06 cron run
across 4 CASH-template recurring bills (VB-0061..64, 5,526.93 EGP)
and restores each bill to DRAFT so it re-enters the overdue panel
for human review.

Every safety valve on: dry-run by default, requires --commit + an
explicit --company-id, refuses bills that don't match the exact
symptom profile (PAID + tied to a recurring template + settlement
JE matches the bill total), and writes a pre-change JSON snapshot
to /tmp so a mistake can be undone by hand.

Usage:
  # Preview only — mutates nothing:
  python scripts/fix_recurring_vbill_autopay_leak.py --company-id 8
  python scripts/fix_recurring_vbill_autopay_leak.py --company-id 8 \\
      --numbers VB-0061,VB-0062,VB-0063,VB-0064

  # Actually apply:
  python scripts/fix_recurring_vbill_autopay_leak.py --company-id 8 --commit

  # Custom target list:
  python scripts/fix_recurring_vbill_autopay_leak.py --company-id 8 \\
      --numbers VB-0099 --commit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Boot into the app's Flask context.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")

DEFAULT_NUMBERS = ["VB-0061", "VB-0062", "VB-0063", "VB-0064"]


def _log(msg: str):
    print(msg, flush=True)


def _snapshot_path() -> Path:
    """Where the pre-change JSON goes. /tmp is fine on the server;
    fall back to the scratchpad on Windows dev boxes."""
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    base = Path("/tmp") if Path("/tmp").exists() else Path.cwd()
    return base / f"vbill_autopay_fix_{stamp}.json"


def _find_bills(company_id: int, numbers: list[str]):
    """Locate the target bills scoped to one company. Raises on any
    lookup that doesn't return exactly one row — a missing bill or
    a cross-company id collision are BOTH reasons to stop."""
    from app.models import VendorBill
    found = []
    for num in numbers:
        rows = VendorBill.query.filter_by(
            company_id=company_id, number=num).all()
        if len(rows) == 0:
            raise SystemExit(
                f"[abort] No VendorBill with number={num} in "
                f"company_id={company_id}")
        if len(rows) > 1:
            raise SystemExit(
                f"[abort] {len(rows)} VendorBills matched number="
                f"{num} in company_id={company_id} — refusing to "
                "guess which one")
        found.append(rows[0])
    return found


def _find_settlement_je(bill_id: int):
    """The cash-out journal posted by post_vendor_bill() when
    payment_method was CASH/BANK. May be None if the source was
    CREDIT (no settlement JE was posted at all)."""
    from app.models import JournalEntry
    return JournalEntry.query.filter_by(
        source_type="vendor_bill_payment", source_id=bill_id,
        is_reversal=False,
    ).order_by(JournalEntry.id.desc()).first()


def _find_posting_je(bill):
    """The main AP-posting journal (accessible directly via
    VendorBill.journal_entry_id, but return None if that FK is
    already NULL — means the script was run before or the bill's
    posting was undone manually)."""
    from app.models import JournalEntry
    if not bill.journal_entry_id:
        return None
    return db.session.get(JournalEntry, bill.journal_entry_id)


def _already_reversed(je):
    """Match reverse_journal's own dedup: it considers an entry
    reversed if ANY active JE points at it via reversal_of."""
    if je is None:
        return False
    from app.models import JournalEntry
    return JournalEntry.query.filter_by(
        reversal_of=je.id, is_active=True).first() is not None


def _validate_bill(bill, posting_je, settlement_je):
    """Pre-flight checks. Every one is a reason to stop and let a
    human look. The script does not try to be clever about edge
    cases — the ticket names 4 specific bills that fit ONE symptom
    profile."""
    from app.models import VendorBillStatus
    reasons = []
    # 1. Must be PAID (auto-payment landed).
    if bill.status != VendorBillStatus.PAID:
        reasons.append(
            f"status={bill.status.value}, expected PAID "
            "(script only fixes bills the cron auto-paid)")
    # 2. Must be recurring-materialised (script isn't for typed bills).
    if bill.recurring_bill_id is None:
        reasons.append(
            "recurring_bill_id is NULL — not a cron-materialised bill; "
            "refuse to touch it")
    # 3. Posting JE must exist.
    if posting_je is None:
        reasons.append(
            "journal_entry_id is NULL — bill has no posting JE; "
            "either already unpicked or never posted")
    # 4. Settlement JE must exist for CASH/BANK (that's the leak
    #    signature). If it doesn't, the bill was never auto-paid.
    if settlement_je is None:
        reasons.append(
            "no settlement JE (source_type=vendor_bill_payment) — "
            "bill wasn't auto-paid; nothing to reverse on the cash side")
    # 5. Settlement amount must equal bill total (within 0.01) so
    #    we know the settlement fully covered it — no partial edge.
    if settlement_je is not None:
        from app.models import JournalLine
        lines = JournalLine.query.filter_by(entry_id=settlement_je.id).all()
        total_dr = sum(float(l.debit or 0) for l in lines)
        if abs(total_dr - float(bill.total or 0)) > 0.01:
            reasons.append(
                f"settlement JE total ({total_dr:.2f}) != bill.total "
                f"({float(bill.total):.2f}) — partial or unusual "
                "settlement; needs manual review")
    return reasons


def _fix_one(bill, *, commit: bool, snapshot: list):
    """Reverse both journals for one bill + reset the bill status.
    Records a pre-change entry in the snapshot list so a mistake
    can be manually undone from /tmp/vbill_autopay_fix_*.json."""
    from app.models import VendorBillStatus
    from app.services.ledger import reverse_journal, LedgerError

    number = bill.number
    posting_je = _find_posting_je(bill)
    settlement_je = _find_settlement_je(bill.id)

    _log(f"\n=== {number} (id={bill.id}) ===")
    _log(f"    current: status={bill.status.value}, "
         f"paid_amount={float(bill.paid_amount or 0):.2f}, "
         f"total={float(bill.total or 0):.2f}, "
         f"recurring_bill_id={bill.recurring_bill_id}, "
         f"posting_JE=#{posting_je.number if posting_je else None}, "
         f"settlement_JE=#{settlement_je.number if settlement_je else None}")

    # Skip cleanly if the script already ran on this bill.
    if bill.status == VendorBillStatus.DRAFT and bill.journal_entry_id is None:
        _log(f"    → already-DRAFT, journal_entry_id=NULL — skipping "
             "(idempotent no-op)")
        return "skip_already_fixed"

    reasons = _validate_bill(bill, posting_je, settlement_je)
    if reasons:
        _log("    ✗ VALIDATION FAILED:")
        for r in reasons:
            _log(f"      · {r}")
        return "skip_validation"

    # Refuse if either JE has already been reversed — running twice
    # would post a compensating entry that puts the ledger at -1x.
    if _already_reversed(settlement_je):
        _log(f"    ✗ settlement JE #{settlement_je.number} already "
             "reversed — refusing to double-reverse")
        return "skip_double_reverse"
    if _already_reversed(posting_je):
        _log(f"    ✗ posting JE #{posting_je.number} already "
             "reversed — refusing to double-reverse")
        return "skip_double_reverse"

    # ─── Snapshot pre-change state for manual rollback ──────
    snapshot.append({
        "number": number, "bill_id": bill.id,
        "status": bill.status.value,
        "paid_amount": float(bill.paid_amount or 0),
        "total": float(bill.total or 0),
        "journal_entry_id": bill.journal_entry_id,
        "posting_je_number": posting_je.number if posting_je else None,
        "settlement_je_id": settlement_je.id if settlement_je else None,
        "settlement_je_number": (settlement_je.number
                                  if settlement_je else None),
        "recurring_bill_id": bill.recurring_bill_id,
    })

    if not commit:
        _log(f"    [dry-run] would reverse settlement JE "
             f"#{settlement_je.number}, then posting JE "
             f"#{posting_je.number}, then reset status → DRAFT, "
             f"paid_amount → 0, journal_entry_id → NULL")
        return "would_fix"

    # ─── ACTUAL MUTATION ────────────────────────────────────
    # Order matters: reverse the settlement (cash-restoring) leg
    # first, then the posting (AP-closing) leg. The other order
    # leaves the intermediate state briefly nonsensical.
    try:
        rev_settle = reverse_journal(settlement_je.id, created_by=None)
        _log(f"    ✓ reversed settlement JE #{settlement_je.number} "
             f"→ reversal JE #{rev_settle.number}")
    except LedgerError as e:
        _log(f"    ✗ settlement reversal failed: {e}")
        return "error_settle"

    try:
        rev_post = reverse_journal(posting_je.id, created_by=None)
        _log(f"    ✓ reversed posting JE #{posting_je.number} "
             f"→ reversal JE #{rev_post.number}")
    except LedgerError as e:
        # If the settlement reversal succeeded but the posting one
        # fails, cash is restored but AP is still zeroed out — we
        # STOP and let a human sort it. Do not attempt to un-reverse
        # the settlement automatically.
        _log(f"    ✗ posting reversal failed: {e}")
        _log(f"       CASH IS RESTORED but AP is still zeroed. "
             "MANUAL FIX REQUIRED for this bill.")
        return "error_post"

    # Reset the VendorBill row itself. reverse_journal doesn't do
    # this — there's no _undo_source_side_effects branch for
    # vendor_bill (verified). Without this reset the ledger says
    # the bill was undone but the bill row still says PAID.
    bill.status = VendorBillStatus.DRAFT
    bill.paid_amount = 0
    bill.journal_entry_id = None
    db.session.commit()
    _log(f"    ✓ reset {number}: status=DRAFT, paid_amount=0, "
         "journal_entry_id=NULL")
    return "fixed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company-id", type=int, required=True,
                        help="Company scope for the numbers lookup.")
    parser.add_argument("--numbers", type=str, default=None,
                        help="Comma-separated bill numbers. "
                             "Defaults to VB-0061,VB-0062,VB-0063,VB-0064.")
    parser.add_argument("--commit", action="store_true",
                        help="Actually mutate. Without this, prints "
                             "intended actions and touches nothing.")
    args = parser.parse_args()

    numbers = ([n.strip() for n in args.numbers.split(",") if n.strip()]
               if args.numbers else DEFAULT_NUMBERS)

    from app import create_app, db as _db
    global db
    db = _db
    app = create_app()

    with app.app_context():
        _log(f"─── MARSOUD-CRON-VBILL-NO-AUTOPAY-01 fix ───")
        _log(f"company_id  : {args.company_id}")
        _log(f"numbers     : {numbers}")
        _log(f"mode        : {'COMMIT' if args.commit else 'DRY-RUN'}")

        try:
            bills = _find_bills(args.company_id, numbers)
        except SystemExit as e:
            print(str(e))
            sys.exit(2)

        snapshot = []
        outcomes = {"fixed": 0, "would_fix": 0,
                    "skip_already_fixed": 0,
                    "skip_validation": 0,
                    "skip_double_reverse": 0,
                    "error_settle": 0, "error_post": 0}
        for bill in bills:
            outcome = _fix_one(bill, commit=args.commit,
                                snapshot=snapshot)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

        # Snapshot always written when there's anything to record.
        if snapshot:
            snap_path = _snapshot_path()
            snap_path.write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False),
                encoding="utf-8")
            _log(f"\nsnapshot: {snap_path}")

        _log(f"\n─── summary ───")
        for k, v in outcomes.items():
            if v:
                _log(f"  {k:25} {v}")

        if not args.commit and outcomes.get("would_fix"):
            _log("\nRe-run with --commit to actually apply.")


if __name__ == "__main__":
    main()
