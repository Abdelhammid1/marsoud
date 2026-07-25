#!/usr/bin/env python3
"""MARSOUD-FIX-VENDOR-BILL-FLOAT (Abdelhamid 2026-07-25).

Bug: "could not convert string to float: ''" when saving a vendor
bill with multiple lines — one of the numeric fields was arriving
as whitespace / empty / comma-formatted.

Checks:
  1. _safe_float(None) → default
  2. _safe_float("") → default
  3. _safe_float("   ") → default  (the crash we saw)
  4. _safe_float("1,000") → 1000.0  (thousand separator)
  5. _safe_float("abc") → default   (garbage doesn't crash)
  6. _safe_float("12.5") → 12.5
  7. _safe_float("١٢٣") → default   (Arabic digits fallback safely)
  8. _safe_float(None, default=7) → 7 (custom default respected)
"""
import os
import sys
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


@check("1. _safe_float(None) → default 0")
def _():
    from app.routes.vendor_bills import _safe_float
    assert _safe_float(None) == 0
    return "OK"


@check("2. _safe_float('') → default 0")
def _():
    from app.routes.vendor_bills import _safe_float
    assert _safe_float("") == 0
    return "OK"


@check("3. _safe_float('   ') → default 0 (was crashing prod)")
def _():
    from app.routes.vendor_bills import _safe_float
    assert _safe_float("   ") == 0
    return "whitespace-only handled"


@check("4. _safe_float('1,000') → 1000.0 (comma-formatted)")
def _():
    from app.routes.vendor_bills import _safe_float
    assert _safe_float("1,000") == 1000.0
    assert _safe_float("1,234.56") == 1234.56
    return "OK"


@check("5. _safe_float('abc') → default 0 (garbage doesn't crash)")
def _():
    from app.routes.vendor_bills import _safe_float
    assert _safe_float("abc") == 0
    assert _safe_float("$$$") == 0
    return "OK"


@check("6. _safe_float('12.5') → 12.5 (happy path)")
def _():
    from app.routes.vendor_bills import _safe_float
    assert _safe_float("12.5") == 12.5
    assert _safe_float("0.001") == 0.001
    assert _safe_float("100") == 100.0
    return "OK"


@check("7. Arabic-Indic digits parse correctly (Python 3 handles them)")
def _():
    from app.routes.vendor_bills import _safe_float
    # Python 3's float() natively parses Arabic-Indic digits, so
    # "١٢٣" → 123.0. Both behaviors are safe (no crash) — the point
    # of this check is to prove no ValueError leaks.
    assert _safe_float("١٢٣") == 123.0
    return "arabic digits → 123 (no crash)"


@check("8. Custom default respected")
def _():
    from app.routes.vendor_bills import _safe_float
    assert _safe_float(None, default=7) == 7
    assert _safe_float("", default=1) == 1
    assert _safe_float("bad", default=99) == 99
    return "OK"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        for label, fn in CHECKS:
            try:
                res = fn()
                print(f"PASS  {label}  ⇒ {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
