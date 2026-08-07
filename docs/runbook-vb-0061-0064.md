# Runbook — VB-0061 → VB-0064 auto-payment leak fix

> **Ticket**: MARSOUD-CRON-VBILL-NO-AUTOPAY-01
> **Date of incident**: 2026-08-06
> **Amount leaked**: **5,526.93 EGP** across 4 vendor bills, all
> auto-paid from cash (account 1110) by the recurring-bills cron
> after a 3-week outage.
> **Company**: 8 (شركة 8)

## What happened

The cron job `process_recurring_vendor_bills()` walked past-due
`RecurringBill` templates and called `materialize_from_recurring()`
without passing `status_target`. That defaulted to `"POSTED"`, so:

1. `post_vendor_bill()` ran on each newly-materialised bill.
2. Because the source templates had `payment_method=CASH`,
   `post_vendor_bill()` posted a **second** journal (Dr Vendor sub /
   Cr Cash 1110) that immediately drained the till.
3. Bills flipped to `PAID`, `paid_amount = total`.

Four bills — `VB-0061`, `VB-0062`, `VB-0063`, `VB-0064` — went
through this path in a single cron tick. No notification, no
review.

## The permanent code fix

Already on this branch and merged in the PR:

- `app/services/recurring_vendor_bills.py` — the cron now passes
  `status_target="DRAFT"` explicitly. Every future recurring
  materialisation lands as DRAFT and appears in the overdue panel
  for human review before any journal is written.
- `app/services/vendor_bills.py` — `materialize_from_recurring`
  default changed from `None` (which resolved to POSTED) to an
  explicit `"POSTED"` string. Any future caller that OMITS the
  argument gets the same behavior; the risky path is now behind
  an explicit choice.
- `tests/audit_recurring_vbill_no_autopay.py` — 5-check regression
  audit pins the new behavior.

## Production data fix — restoring the 4 bills

The code fix stops future incidents; the 4 stray bills still need
to be unwound. Script:
`scripts/fix_recurring_vbill_autopay_leak.py`.

### Prerequisites

- Server is running this branch (or a merge that contains it).
- `systemctl restart accountant` completed cleanly.
- `flask db upgrade` shows no pending migrations (this ticket
  ships no migration — sanity check only).

### Step 1: dry-run

```bash
python scripts/fix_recurring_vbill_autopay_leak.py --company-id 8
```

Expected output (per bill):

```
=== VB-0061 (id=XXX) ===
    current: status=PAID, paid_amount=1500.00, total=1500.00, ...
    [dry-run] would reverse settlement JE #YYYY, then posting JE
              #ZZZZ, then reset status → DRAFT, paid_amount → 0,
              journal_entry_id → NULL
```

Repeated 4 times. Summary at the bottom shows `would_fix: 4`.

**If any bill fails validation** (wrong status, missing settlement
JE, mismatched totals, already reversed) the script logs a
`✗ VALIDATION FAILED` block with the reason. **Do NOT proceed to
step 2** until every bill in the target list shows `would_fix`.

### Step 2: apply

```bash
python scripts/fix_recurring_vbill_autopay_leak.py --company-id 8 --commit
```

For each bill, expected:
- Reverses the settlement JE (cash-restoring leg first).
- Reverses the posting JE (AP-closing leg second).
- Resets `bill.status = DRAFT`, `bill.paid_amount = 0`,
  `bill.journal_entry_id = NULL`.
- Commits.

Summary shows `fixed: 4`.

A pre-change snapshot is written to
`/tmp/vbill_autopay_fix_<timestamp>.json` — keep it until you've
verified everything.

### Step 3: verify

1. **Cash balance restored.**
   Compare account **1110** balance to the 2026-08-05 snapshot.
   The difference should be **+5,526.93 EGP** vs the post-cron
   balance (i.e. exactly the sum that was drained).

2. **Bills back in the overdue panel.**
   Open `/reports/ap-aging` or the vendor-bills index. VB-0061..64
   should appear as DRAFT bills tied to their recurring templates.

3. **Journals traceable.**
   For each of the 4 bills, `/journals/` should show:
   - Original posting JE (now with `is_active=False` / reversed)
   - Original settlement JE (now reversed)
   - Two new reversal JEs, one per side
   Nothing else changed.

4. **Idempotency.** Re-run the script:
   ```
   python scripts/fix_recurring_vbill_autopay_leak.py --company-id 8 --commit
   ```
   Every bill should log `skip_already_fixed`. Summary:
   `skip_already_fixed: 4`.

### Step 4: monitor

The next cron tick materialises the same 4 recurring templates
(and any others due) as **DRAFT** bills — they should appear in
the overdue panel without any cash movement. The accountant then
posts each one manually when ready.

## If something goes wrong mid-script

The script writes the pre-change snapshot **before** mutating. If
a bill ends up in a bad state:

1. Open `/tmp/vbill_autopay_fix_<timestamp>.json` — has the exact
   pre-state for every bill it touched.
2. Reverse the reversal JEs (they'll show up in `/journals/`
   filtered by `source_type=vendor_bill_payment` or
   `vendor_bill`) to restore the ORIGINAL state.
3. Restore `bill.status`, `paid_amount`, `journal_entry_id` from
   the snapshot via `flask shell`:
   ```python
   from app.models import VendorBill, VendorBillStatus
   from app import db
   b = VendorBill.query.get(<bill_id>)
   b.status = VendorBillStatus.<original>
   b.paid_amount = <original>
   b.journal_entry_id = <original>
   db.session.commit()
   ```

The script also refuses to double-reverse (the `_already_reversed`
guard on both JEs) so a second run in the wrong state is a no-op,
not a compound mistake.

## Not covered by this runbook

- **The manual "اعمل الفاتورة" button** on the recurring-bills
  dashboard (`app/routes/recurring_bills.py`) has the same
  auto-pay bug shape. The ticket explicitly said manual invocations
  stay as-is. If operators want the manual path to defer to DRAFT
  too, that's a follow-up ticket.
- **`_undo_source_side_effects`** in `app/services/ledger.py` has
  no branch for `vendor_bill*` source types. Reversing a vendor
  bill's journal from `/journals/` (outside this script) will NOT
  flip the bill's status back to DRAFT — you'd have to reset the
  bill manually. Ticket for a broader fix is queued separately.
