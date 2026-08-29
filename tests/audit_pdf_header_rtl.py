#!/usr/bin/env python3
"""MARSOUD-TKT-PDF-HEADER-RTL-HOTFIX (Abdelhamid 2026-08-29) — every
ReportLab PDF's header must render right-anchored, matching the body.

Customer bug: JE-0158 PDF from prod (2026-08-29) had the header
(logo + company name + title `قيد يومية — JE-0158` + subtitle
`التاريخ: 2026-09-05 · جنيه مصري`) drawn LEFT-anchored, while the
body used `drawRightString` (right-anchored). Same PDF had two
layout modes fighting. Arabic-primary document → the header should
be on the right side, same as the body.

Fix: rewrote `_pdf_header` in `services/export.py` to mirror every
draw to the right edge (drawRightString at 19.5cm and logo at
19.8cm-right). One function change fixed ALL 14 caller sites at
once (balance sheet, income statement, VAT, journal entries,
journal lists, payroll, expense summary, legacy invoice fallback,
etc.).

This audit is the regression net. Static file read only, no DB, <1s.
If a future refactor of `_pdf_header` reintroduces left-anchored
draws, the audit fails loudly before it ships to another customer.

Checks:
  1. `_pdf_header` in `services/export.py` uses drawRightString on
     the four right-anchored draws (company name, sub-line, title,
     period). Zero left-anchored `drawString` calls remain inside
     that function.
  2. Logo, when present, is positioned at the right edge of the
     navy band (drawImage x-coord ≥ 15cm from the left paper edge —
     A4 is 21cm, midpoint 10.5cm; right half starts at 10.5cm and
     the logo occupies the last ~3cm so its left edge is ≥ 15cm).
  3. The four ar()-carrying arguments still flow into drawRightString
     (not stripped by a refactor that forgot to reshape the Arabic).
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


def _read(relpath):
    return (ROOT / relpath).read_text(encoding="utf-8")


def _strip_comments(src):
    """Remove Python `#` comments so retirement-doc notes in the source
    (documenting `was drawString(logo_x, ...)`) don't false-positive the
    audit. Also strip triple-quoted docstrings inside the function body
    for the same reason. Simple line-scanner is good enough for a Python
    file with no `#` in string literals in the function we care about."""
    # Strip full-line `#` comments and inline `# …` tails.
    lines = []
    for line in src.split("\n"):
        # Simple: cut everything after the first standalone `#` that
        # isn't inside a string literal. Since our target function has
        # no `#` inside strings, a naive strip is safe here.
        stripped = re.sub(r"#[^\n]*$", "", line)
        lines.append(stripped)
    return "\n".join(lines)


def _extract_pdf_header_body(src):
    """Return the body of `def _pdf_header(...)` up to (but not
    including) the next top-level `def`. That's the slice the audit
    reasons about — we don't want draws in OTHER functions (which
    intentionally use drawString for section bars, table cells, etc.)
    to interfere with the check."""
    m = re.search(
        r"^def _pdf_header\([^)]*\):\n(.*?)(?=^def \w)",
        src, re.MULTILINE | re.DOTALL,
    )
    assert m, "_pdf_header function not found in services/export.py"
    return m.group(1)


@check("1. _pdf_header uses drawRightString on all four text draws")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    body = _extract_pdf_header_body(src)

    # Must call drawRightString for the four right-anchored draws.
    right_calls = re.findall(r"p\.drawRightString\(", body)
    assert len(right_calls) >= 4, (
        f"_pdf_header has only {len(right_calls)} drawRightString "
        "calls; expected ≥4 (company name + sub-line + title + period)"
    )
    # Must NOT call plain drawString anywhere inside — that was the
    # left-anchored regression pattern.
    left_calls = re.findall(r"p\.drawString\(", body)
    assert not left_calls, (
        f"_pdf_header contains {len(left_calls)} left-anchored "
        f"p.drawString(...) call(s) — regressed to the pre-hotfix "
        f"layout. Use drawRightString for RTL-anchored text."
    )
    return f"{len(right_calls)} drawRightString, 0 drawString"


@check("2. logo (if present) is positioned in the right half of the page")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    body = _extract_pdf_header_body(src)
    # The drawImage call in _pdf_header positions the logo. Its x-coord
    # must be ≥ 15cm (right half of a 21cm-wide A4 page). Pre-hotfix
    # this was 1.2cm (left edge).
    m = re.search(r"p\.drawImage\(\s*img\s*,\s*([^\s,]+)\s*,", body)
    assert m, "drawImage call for the logo not found in _pdf_header"
    x_expr = m.group(1).strip()
    # Try to evaluate common forms like `logo_left`, `19.8 * cm`.
    # If it's a variable, look up its assignment above.
    if "cm" in x_expr:
        # e.g. "19.8 * cm" → extract 19.8
        num_m = re.match(r"([\d.]+)\s*\*\s*cm", x_expr)
        assert num_m, f"could not parse logo x-coord: {x_expr!r}"
        x_cm = float(num_m.group(1))
    else:
        # It's a variable — resolve it against the function body. Two
        # supported forms: (a) `NAME = N * cm` literal, or (b) an
        # arithmetic form like `logo_left = logo_right - logo_width`
        # where both operands are themselves `N * cm` literals.
        lit_m = re.search(
            re.escape(x_expr) + r"\s*=\s*([\d.]+)\s*\*\s*cm", body
        )
        if lit_m:
            x_cm = float(lit_m.group(1))
        else:
            # Try the arithmetic form: NAME = OTHER - OTHER
            arith_m = re.search(
                re.escape(x_expr) + r"\s*=\s*(\w+)\s*-\s*(\w+)", body
            )
            assert arith_m, (
                f"logo x-coord is `{x_expr}` but its assignment isn't a "
                f"literal `N * cm` or an `A - B` subtraction the audit "
                f"can resolve. If the coord is dynamic, add an assertion "
                f"here that computes its value."
            )
            lhs_name, rhs_name = arith_m.group(1), arith_m.group(2)
            lhs = re.search(
                re.escape(lhs_name) + r"\s*=\s*([\d.]+)\s*\*\s*cm", body
            )
            rhs = re.search(
                re.escape(rhs_name) + r"\s*=\s*([\d.]+)\s*\*\s*cm", body
            )
            assert lhs and rhs, (
                f"could not resolve `{lhs_name}` and/or `{rhs_name}` "
                f"to `N * cm` literals for the audit"
            )
            x_cm = float(lhs.group(1)) - float(rhs.group(1))

    assert x_cm >= 15.0, (
        f"logo drawImage x-coord = {x_cm}cm — in the LEFT half of the "
        f"page (A4 is 21cm, right half starts at 10.5cm; logos should "
        f"be ≥ 15cm so their left edge sits in the right ~30% of the "
        f"page for an Arabic document)"
    )
    return f"logo left edge at {x_cm}cm (right half of page)"


@check("3. drawRightString calls carry ar()-processed Arabic")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    body = _extract_pdf_header_body(src)
    # Count how many drawRightString calls receive an `ar(...)` argument.
    # Every one should — the four draws are all Arabic-capable text.
    ar_wrapped = re.findall(r"p\.drawRightString\([^)]*\bar\(", body)
    right_calls = re.findall(r"p\.drawRightString\(", body)
    assert len(ar_wrapped) == len(right_calls), (
        f"only {len(ar_wrapped)}/{len(right_calls)} drawRightString "
        f"calls in _pdf_header wrap their text in ar() — a bare "
        f"drawRightString skips the Arabic reshape+bidi pipeline and "
        f"the text will render as isolated character forms."
    )
    return f"all {len(right_calls)} drawRightString calls pipe through ar()"


def main():
    passed = failed = 0
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
