#!/usr/bin/env python3
"""MARSOUD-UI-DESIGN-SYSTEM (2026-08-18) — TKT-06/07/08/09 audit.

Verifies the design-system pass is wired end-to-end:

  A. `_design_tokens.html` exists and defines the canonical
     `--ms-*` variables.
  B. Both `base.html` and `admin/base.html` include it BEFORE
     their own inline `<style>` block (so the tokens are
     available when the classes below try to read them).
  C. `_macros.html` defines the shared macros (empty_state,
     stat_tile, status_badge, page_header).
  D. Dashboard's `#dash-root` scope no longer hard-codes the
     stray `#159b54` — it aliases `--d-green` to `--ms-brand`.
  E. Touched page-templates (dashboard + vendor_bills/index +
     invoices/index) use the `.empty-state` class instead of
     the ad-hoc inline patterns.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL  = ROOT / "app" / "templates"

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── A. Tokens file ───────────────────────────────────────────────────
@check("A1: _design_tokens.html defines the canonical --ms-* variables")
def A1():
    p = TPL / "_design_tokens.html"
    assert p.exists(), "missing _design_tokens.html"
    src = p.read_text(encoding="utf-8")
    for token in ("--ms-brand:", "--ms-brand-dark:", "--ms-ink-1:",
                  "--ms-surface:", "--ms-radius-lg:",
                  "--ms-shadow-md:", "--ms-font-body:"):
        assert token in src, f"missing token {token}"


@check("A2: _design_tokens.html defines shared primitives")
def A2():
    src = (TPL / "_design_tokens.html").read_text(encoding="utf-8")
    for klass in (".empty-state", ".htmx-indicator", ".ms-spinner",
                  ".stat-tile", ".page-header"):
        assert klass in src, f"missing primitive {klass}"


# ─── B. Include wiring ────────────────────────────────────────────────
@check("B1: base.html includes _design_tokens.html before the tenant style block")
def B1():
    src = (TPL / "base.html").read_text(encoding="utf-8")
    inc_pos = src.find("{% include '_design_tokens.html' %}")
    # The tenant's own inline design system starts with `body {`
    # right after the include. Use that as the anchor — searching
    # for the bare `<style>` string can false-hit on the word
    # inside a nearby comment.
    body_pos = src.find("body {")
    assert inc_pos != -1, "base.html doesn't include _design_tokens.html"
    assert body_pos != -1, "base.html has no body { rule"
    assert inc_pos < body_pos, (
        "_design_tokens.html included AFTER the base style block")


@check("B2: admin/base.html includes _design_tokens.html")
def B2():
    src = (TPL / "admin" / "base.html").read_text(encoding="utf-8")
    assert "_design_tokens.html" in src, (
        "admin/base.html doesn't include _design_tokens.html")


# ─── C. Macros ────────────────────────────────────────────────────────
@check("C1: _macros.html defines empty_state, stat_tile, status_badge, page_header")
def C1():
    p = TPL / "_macros.html"
    assert p.exists(), "missing _macros.html"
    src = p.read_text(encoding="utf-8")
    for macro in ("macro empty_state(", "macro stat_tile(",
                  "macro status_badge(", "macro page_header("):
        assert macro in src, f"missing {macro}"


# ─── D. Dashboard palette unified ─────────────────────────────────────
@check("D1: dashboard/index.html aliases --d-green to --ms-brand (no stray #159b54)")
def D1():
    src = (TPL / "dashboard" / "index.html").read_text(encoding="utf-8")
    # The old private palette had `--d-green:#159b54;`. Post-pass,
    # that assignment must be gone (an alias `var(--ms-brand)`
    # replaces it).
    assert "--d-green:#159b54" not in src, (
        "dashboard still hard-codes #159b54 for --d-green")
    assert "--d-green:var(--ms-brand)" in src, (
        "dashboard should alias --d-green to var(--ms-brand)")


# ─── E. Touched pages adopt .empty-state (or its ui.empty_state macro) ─
# MARSOUD-TKT-P0-05-MACROS (2026-08-28) — loosened both checks to accept
# `ui.empty_state(` alongside the literal `class="empty-state"`. Rationale
# below the check: the macro expands to a <div class="empty-state">, so a
# macro call satisfies the audit's original intent ("touched pages use the
# empty-state primitive") more strongly than the raw class. When the ratchet
# grows and every page moves to the macro form, the literal-string branch
# of the OR can be retired.
def _has_empty_state(src):
    return 'class="empty-state"' in src or 'ui.empty_state(' in src


@check("E1: dashboard has 3 .empty-state renders (overdue + upcoming + AR)")
def E1():
    src = (TPL / "dashboard" / "index.html").read_text(encoding="utf-8")
    count = src.count('class="empty-state"') + src.count('ui.empty_state(')
    assert count >= 3, f"expected ≥3 empty-state blocks (literal or macro), got {count}"


@check("E2: vendor_bills/index.html + invoices/index.html use .empty-state")
def E2():
    for path in ("vendor_bills/index.html", "invoices/index.html"):
        src = (TPL / path).read_text(encoding="utf-8")
        assert _has_empty_state(src), (
            f"{path}: no .empty-state literal or ui.empty_state(…) call")


# ─── Runner ───────────────────────────────────────────────────────────
def main():
    failed = []
    for label, fn in CHECKS:
        try:
            fn()
            print(f"  [OK]   {label}")
        except Exception as e:
            failed.append((label, e))
            print(f"  [FAIL] {label}\n         -> {e}")
    total = len(CHECKS)
    ok = total - len(failed)
    print()
    print(f"{ok}/{total} OK" if not failed
          else f"{ok}/{total} -- {len(failed)} FAILED")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
