#!/usr/bin/env python3
"""MARSOUD-TKT-P2-A11Y (Abdelhamid 2026-08-28) — accessibility baseline
must stay green.

Design audit 2026-08-28 findings 08, 09, 10 shipped in one ticket:

  08. `text-slate-400` (#94A3B8 → 2.85:1 on white) failed WCAG AA for
      normal + large text. Was used at 401 sites across 165 templates.
      Bulk-swept to `text-slate-500` (#64748B → 4.68:1, passes AA even
      for small text). --ms-ink-4 token in _design_tokens.html moved
      from #94a3b8 → #64748b at the same time.
  09. `:focus-visible` outline added to every button primitive + nav
      row in both tenant + admin shells. `:focus-visible` (not
      `:focus`) so mouse clicks stay clean; keyboard users see a
      3px green ring with 2px offset.
  10. `@media (prefers-reduced-motion: reduce)` block added to
      _design_tokens.html. Global reset: cuts animation-duration and
      transition-duration to 0.01ms, disables scroll smoothness.
      Both shells inherit the tokens file so neither's inline <style>
      block had to change.

This audit is the regression net — every check is a static file read +
substring assertion (no DB, no app bootstrap, <1s). If a future ticket
reintroduces a `text-slate-400` class, removes the focus-visible rule,
or removes the motion block, the check fails loudly instead of shipping
silently past a screen reader / keyboard-first user.
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
    """Remove Jinja `{# … #}`, JS `//` line, and CSS `/* … */` comments so
    substring checks don't false-positive on retirement-doc notes in the
    source ("was text-slate-400, now text-slate-500"). Order matters —
    strip JS `//` before CSS `/* */` so a JS comment containing `/*`
    (e.g. inside a `admin/*.html templates …` note) can't open a spurious
    CSS-comment match that greedy-runs to the next `*/` many lines away.

    Copied from tests/audit_brand_tokens_consumed.py so both audits use
    the same idiom. Small enough that the duplication is cleaner than
    a shared helper module for two callers.
    """
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


@check("1. no `text-slate-400` remaining in any template (live code only)")
def _():
    misses = []
    total_files = 0
    for path in sorted((ROOT / "app" / "templates").rglob("*.html")):
        total_files += 1
        rel = path.relative_to(ROOT).as_posix()
        src = _strip_comments(path.read_text(encoding="utf-8"))
        # Match the class as a whole token so we can't be tripped by
        # a hypothetical `text-slate-4000` (none exist today).
        for m in re.finditer(r"\btext-slate-400\b", src):
            # Report file:approximate-line for the first hit per file.
            line = src[:m.start()].count("\n") + 1
            misses.append(f"{rel}:{line}")
            break  # one hit per file is enough to fail
    assert not misses, \
        f"{len(misses)} template(s) still use text-slate-400 " \
        f"(#94A3B8 = 2.85:1 fails WCAG AA): {misses[:5]}" \
        + (f" … and {len(misses)-5} more" if len(misses) > 5 else "")
    return f"swept clean across {total_files} template files"


@check("2. --ms-ink-4 in tokens now passes contrast")
def _():
    src = _read("app/templates/_design_tokens.html")
    # Value line must be #64748b (matches --ms-ink-3, WCAG AA compliant).
    assert re.search(r"--ms-ink-4:\s*#64748b\b", src, re.IGNORECASE), \
        "--ms-ink-4 is not set to #64748b — check _design_tokens.html"
    # And the previous failing value must be gone from the live rule.
    stripped = _strip_comments(src)
    assert re.search(r"--ms-ink-4:\s*#94a3b8\b", stripped, re.IGNORECASE) is None, \
        "--ms-ink-4 is still #94a3b8 in live CSS (fails WCAG AA)"
    return "--ms-ink-4: #64748b (AA-compliant)"


@check("3. both shells declare :focus-visible on button primitives")
def _():
    for path in ("app/templates/base.html",
                 "app/templates/admin/base.html"):
        src = _strip_comments(_read(path))
        # Must have the pseudo-class on .btn-primary (the CTA that
        # matters most; if this one is there, the whole compound
        # selector is likely there).
        assert ".btn-primary:focus-visible" in src, \
            f"{path} does not declare .btn-primary:focus-visible " \
            "— WCAG 2.4.7 keyboard users have no visible focus indicator"
        # And a nav-link focus rule so sidebar navigation is keyboard-
        # discoverable.
        assert ".nav-link:focus-visible" in src, \
            f"{path} does not declare .nav-link:focus-visible"
        # The rule must actually paint SOMETHING visible — a bare
        # `:focus-visible { }` with no styles is worse than nothing.
        # Check for either an `outline:` or `box-shadow:` inside a
        # focus-visible rule block.
        focus_pattern = re.compile(
            r"\.btn-primary:focus-visible[^{]*\{([^}]*)\}",
            re.DOTALL,
        )
        m = focus_pattern.search(src)
        assert m, f"{path} .btn-primary:focus-visible rule body not found"
        body = m.group(1)
        assert "outline" in body or "box-shadow" in body, \
            f"{path} .btn-primary:focus-visible rule has neither " \
            "outline nor box-shadow — invisible focus"
    return "both shells: .btn-primary + .nav-link keyed to :focus-visible"


@check("4. _design_tokens.html respects prefers-reduced-motion")
def _():
    src = _strip_comments(_read("app/templates/_design_tokens.html"))
    assert "@media (prefers-reduced-motion: reduce)" in src, \
        "_design_tokens.html has no @media (prefers-reduced-motion: " \
        "reduce) block — vestibular-sensitive users still see every " \
        "hover translate, flash slide-in, and spinner spin"
    # The block must actually neutralise motion — check for at least
    # the `animation-duration` reset, which is the load-bearing rule
    # (transitions are usually short enough to not need it, but
    # infinite spinners must be broken).
    motion_block = re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([^}]*\{[^}]*\})",
        src, re.DOTALL,
    )
    assert motion_block, "reduce-motion @media block malformed"
    body = motion_block.group(1)
    assert "animation-duration" in body, \
        "reduce-motion block missing animation-duration reset " \
        "(the .ms-spinner and other infinite animations still run)"
    assert "transition-duration" in body, \
        "reduce-motion block missing transition-duration reset " \
        "(hover translates and sidebar collapse still animate)"
    return "reduce-motion neutralises animation-duration + transition-duration"


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
