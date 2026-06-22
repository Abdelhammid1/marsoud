#!/usr/bin/env python3
"""MARSOUD-COA-LEGACY-1140 — verifies the code no longer references the
legacy inventory account code 1140 anywhere outside the migration that
renamed it to 1300.

Background (from Abdelhamid):
  During the June 2026 CoA cleanup, one orphan journal entry was found
  on account 1140 (the old Inventory code) in company #10 — a single
  VB-0001 bill dated 2026-06-04 for 800. Every bill after the ERP-01
  cutover (13–16 June) posts to 1300. The orphan dates from BEFORE the
  rename and is preserved intentionally to keep the audit trail intact.

What this audit does:
  - Walks the app/ tree and asserts no Python file mentions '1140' as
    an account-code literal (except in comments referencing the
    migration). Migration files are excluded — the rename history must
    stay.
  - Confirms vendor_bills.py, inventory.py, reports.py, seed_coa.py
    all use 1300 for inventory operations.
  - Lights up red if any future refactor reintroduces 1140 in the app
    layer, so we don't silently fork a stale account code path.
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


@check("1. No app-layer Python file uses '1140' as an account-code literal")
def _():
    hits = []
    for p in _walk_app_files():
        txt = p.read_text()
        for ln_no, line in enumerate(txt.splitlines(), start=1):
            # Match quoted "1140" or '1140' but ignore docstring + comment lines
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"[\"']1140[\"']", line):
                hits.append(f"{p.relative_to(ROOT)}:{ln_no}: {stripped[:120]}")
    assert not hits, "1140 reintroduced in app code:\n  " + "\n  ".join(hits)
    return f"scanned {sum(1 for _ in _walk_app_files())} files — no '1140' literals"


@check("2. seed_coa.py seeds 1300 as the Inventory account (not 1140)")
def _():
    src = (ROOT / "app/services/seed_coa.py").read_text()
    # The seed tuple format is ("CODE", "Name", "اسم", AccountType.X, "parent")
    assert re.search(r'\(\s*"1300"\s*,\s*"Inventory"', src), \
        "seed_coa missing the 1300 Inventory row"
    # And it must NOT seed 1140 anymore
    assert "\"1140\"" not in src and "'1140'" not in src, \
        "seed_coa still references 1140"
    return "1300=Inventory seeded; 1140 not seeded"


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
    src = (ROOT / "app/services/reports.py").read_text()
    assert "\"1300\"" in src, "reports.py doesn't reference 1300"
    # The current-assets list used by dashboard_metrics
    assert "[\"1110\", \"1120\", \"1130\", \"1300\", \"1150\"]" in src, \
        "current-assets account list missing or out of date"
    assert "\"1140\"" not in src and "'1140'" not in src, \
        "reports.py still references 1140 as an account code"
    return "1300 in current-assets list; 1140 absent"


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
