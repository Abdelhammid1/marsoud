#!/usr/bin/env python3
"""MARSOUD-TKT-P0-11-EMOJI-ARIA (Abdelhamid 2026-08-29) — decorative
emoji in the shell must carry aria-hidden.

Design audit 2026-08-28 finding P0-11: sidebar navigation icons in both
shells (`base.html` + `admin/base.html`) render emoji directly as
`<span class="text-base">{{ icon }}</span>` next to their Arabic label.
Screen readers announce the emoji name in the reader's locale (VoiceOver
in English reads `📊` as "bar chart"), producing bilingual noise before
every sidebar item. The Arabic label already carries the meaning; the
emoji is decoration.

Fix: mark every decorative-emoji span with `aria-hidden="true"` so the
screen reader skips it and announces only the label. Applied across the
sidebar `.nav-link` spans, the section-header icon span, the shield tile,
and the top-bar user-menu emoji spans.

This audit is the regression net. Static file read only, no DB, <1s. If
a future ticket adds a new nav-icon render site and forgets aria-hidden,
or if a refactor accidentally strips it from an existing site, the audit
fails loudly instead of quietly regressing every Arabic-only screen
reader user.

Checks:
  1. Every `<span class="text-base">{{ icon }}</span>` and
     `<span class="text-base">{{ section_icon }}</span>` template variable
     render in each shell carries `aria-hidden="true"`.
  2. Each shell contains at least a baseline count of `aria-hidden="true"`
     attributes, so a subtle strip below the threshold is caught.
  3. The admin shield tile `🛡️` div carries `aria-hidden="true"`.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "app" / "templates"


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _read(relpath):
    return (TPL / relpath).read_text(encoding="utf-8")


@check("1. every `{{ icon }}` / `{{ section_icon }}` span is aria-hidden")
def _():
    """Match every span whose content is exactly a `{{ icon }}` or
    `{{ section_icon }}` template variable, and assert it carries
    aria-hidden="true"."""
    misses = []
    for shell in ("base.html", "admin/base.html"):
        src = _read(shell)
        # Non-greedy match on the span, then inspect its attribute list.
        for m in re.finditer(
            r'<span([^>]*)>\s*\{\{\s*(icon|section_icon)\s*\}\}\s*</span>',
            src,
        ):
            attrs = m.group(1)
            if 'aria-hidden="true"' not in attrs:
                line = src[:m.start()].count("\n") + 1
                misses.append(f"{shell}:{line} → {m.group(0)[:60]}")
    assert not misses, \
        "nav-icon spans missing aria-hidden:\n  " + "\n  ".join(misses)
    return "all nav-icon and section-icon spans are aria-hidden"


@check("2. each shell carries at least a baseline aria-hidden count")
def _():
    """Baseline guard so a stealth refactor can't drop aria-hidden below
    the number this ticket landed. If new emoji-adornments are added, the
    baseline grows; if the guard fires, that's the signal to increment
    the baselines here after auditing what actually changed."""
    baselines = {
        # base.html landed with: 3 nav-icon / section-icon spans + 1
        # shield tile + 5 user-menu emoji spans = 9 sites.
        "base.html": 9,
        # admin/base.html landed with: 1 shield tile + 1 nav-icon span.
        "admin/base.html": 2,
    }
    for shell, expected in baselines.items():
        src = _read(shell)
        actual = src.count('aria-hidden="true"')
        assert actual >= expected, (
            f"{shell}: expected ≥{expected} aria-hidden=\"true\" attributes, "
            f"got {actual}. Either a decorative-emoji site lost its "
            f"aria-hidden, or the baseline needs updating in this audit."
        )
    return "both shells at or above the ticket's baseline"


@check("3. the admin shield tile 🛡️ div is aria-hidden")
def _():
    """The shield tile is a purely-decorative brand tile in the admin
    sidebar header. It carries no accessible text of its own (the
    `لوحة المالك` label sits in a sibling div), so screen readers
    would announce "shield emoji" for zero information."""
    src = _read("admin/base.html")
    # The div is `<div class="… from-brand-600 to-brand-800 …" aria-hidden="true">🛡️</div>`.
    assert re.search(
        r'<div[^>]*from-brand-600[^>]*aria-hidden="true"[^>]*>\s*🛡️',
        src,
    ), "admin shield-tile div is missing aria-hidden=\"true\""
    return "shield tile hidden from screen readers"


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
