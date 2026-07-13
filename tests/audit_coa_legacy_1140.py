#!/usr/bin/env python3
"""MARSOUD-COA-LEGACY-1140 — verifies the code doesn't reuse the legacy
inventory account code 1140 for INVENTORY. 1140 is now legitimately
seeded as "Notes Receivable" (أوراق قبض) after the 2026-06-30 CoA
rebuild — the audit was retargeted 2026-07-13 to allow that use but
still flag any inventory-related reference to 1140.

Background (from Abdelhamid):
  During the June 2026 CoA cleanup, one orphan journal entry was found
  on account 1140 (the old Inventory code) in company #10 — a single
  VB-0001 bill dated 2026-06-04 for 800. Every bill after the ERP-01
  cutover (13–16 June) posts to 1300. The orphan dates from BEFORE the
  rename and is preserved intentionally to keep the audit trail intact.

  Then on 2026-06-30 the CoA rebuild (b87274d) reintroduced the code
  1140 — but as "Notes Receivable" under the 1100 AR header. Same
  digits, a completely different account. That's why the earlier
  "no 1140 anywhere" check went stale.

What this audit does:
  - In seed_coa.py: 1140 MUST be labelled "Notes Receivable" (never
    "Inventory"). 1300 remains the Inventory row.
  - In inventory-touching services (vendor_bills.py, inventory.py,
    reports.py): 1140 must NOT appear at all — those services should
    only reference 1300 for inventory work. Any regression that pipes
    inventory through 1140 lights this red.
  - Lists reports.py's current-assets rollup so a future edit that
    silently drops 1300 (or adds 1140) is caught.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _walk_app_files():
    """Yield every .py file in app/ — skip __pycache__ + test stubs."""
    for p in ROOT.glob("app/**/*.py"):
        if "__pycache__" in str(p):
            continue
        yield p


@check("1. No inventory-touching service references '1140'")
def _():
    # AUDIT SYNC 2026-07-13 — narrowed from "no app file uses 1140" to
    # "no inventory-touching service uses 1140". 1140 is now a valid
    # Notes Receivable account seeded in seed_coa.py (post CoA rebuild
    # b87274d). We still guard the inventory paths — a stray 1140 in
    # inventory.py or vendor_bills.py would signal a real regression.
    _INVENTORY_SERVICES = (
        "app/services/inventory.py",
        "app/services/vendor_bills.py",
        "app/services/reports.py",
    )
    hits = []
    for rel in _INVENTORY_SERVICES:
        p = ROOT / rel
        if not p.exists():
            continue
        for ln_no, line in enumerate(p.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"[\"']1140[\"']", line):
                hits.append(f"{rel}:{ln_no}: {stripped[:120]}")
    assert not hits, \
        "1140 leaked into an inventory service:\n  " + "\n  ".join(hits)
    return f"scanned {len(_INVENTORY_SERVICES)} inventory services — no '1140'"


@check("2. seed_coa.py: 1300=Inventory, 1140=Notes Receivable (not Inventory)")
def _():
    # AUDIT SYNC 2026-07-13 — 1140 is now legitimately seeded as Notes
    # Receivable. The invariant is that its label must remain "Notes
    # Receivable" (never regress back to "Inventory"), and 1300 must
    # stay Inventory.
    src = (ROOT / "app/services/seed_coa.py").read_text()
    assert re.search(r'\(\s*"1300"\s*,\s*"Inventory"', src), \
        "seed_coa missing the 1300 Inventory row"
    assert re.search(r'\(\s*"1140"\s*,\s*"Notes Receivable"', src), \
        "seed_coa's 1140 row is missing or no longer labelled Notes Receivable"
    # Belt-and-braces: 1140 must not appear as Inventory anywhere.
    assert not re.search(r'\(\s*"1140"\s*,\s*"Inventory"', src), \
        "seed_coa regressed — 1140 relabelled as Inventory"
    return "1300=Inventory + 1140=Notes Receivable both seeded"


@check("3. vendor_bills.py enforces INVENTORY lines map to 1300")
def _():
    src = (ROOT / "app/services/vendor_bills.py").read_text()
    assert "BillLineType.INVENTORY: \"1300\"" in src, \
        "INVENTORY → 1300 mapping missing"
    assert "acc.code != \"1300\"" in src, \
        "missing the 'must be 1300' guard for INVENTORY lines"
    assert "\"1140\"" not in src, "vendor_bills.py still references 1140"
    return "INVENTORY bill lines locked to 1300 with explicit guard"


@check("4. inventory.py uses 1300 everywhere it touches the GL")
def _():
    src = (ROOT / "app/services/inventory.py").read_text()
    # Multiple call sites should all use 1300
    n_1300 = src.count("\"1300\"") + src.count("'1300'")
    assert n_1300 >= 4, \
        f"expected ≥4 references to 1300 in inventory.py, found {n_1300}"
    assert "\"1140\"" not in src and "'1140'" not in src, \
        "inventory.py still references 1140"
    return f"{n_1300} references to 1300; no 1140"


@check("5. reports.py classifies 1300 as inventory in balance-sheet/aging")
def _():
    # AUDIT SYNC 2026-07-13 — the current-assets rollup gained 1280
    # (Input VAT — Recoverable) as part of the CoA rebuild, which is
    # correct: recoverable input VAT belongs on the current-assets
    # line of the balance sheet.
    src = (ROOT / "app/services/reports.py").read_text()
    assert "\"1300\"" in src, "reports.py doesn't reference 1300"
    assert '["1110", "1120", "1130", "1280", "1300", "1150"]' in src, \
        "current-assets account list missing or out of date"
    # 1140 must NEVER appear in reports.py (inventory-touching service).
    assert "\"1140\"" not in src and "'1140'" not in src, \
        "reports.py references 1140 — likely a legacy inventory bug"
    return "1300 in current-assets list; 1140 absent from inventory paths"


@check("6. The historical rename migration is still present (audit trail)")
def _():
    f = ROOT / "migrations/versions/p4d1a8b6c5e7_rename_inventory_1140_1300.py"
    assert f.exists(), \
        "migration that renamed 1140 → 1300 is missing — audit trail broken"
    txt = f.read_text()
    assert "1140" in txt and "1300" in txt, \
        "rename migration is missing the old/new account codes"
    return "rename migration preserved"


def main():
    passed = failed = 0
    for label, fn in CHECKS:
        try:
            msg = fn()
            print(f"\033[92mPASS\033[0m  {label}")
            if msg:
                print(f"        {msg}")
            passed += 1
        except Exception as e:
            print(f"\033[91mFAIL\033[0m  {label}")
            print(f"        {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"  {passed}/{passed + failed} checks passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
