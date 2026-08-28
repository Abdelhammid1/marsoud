#!/usr/bin/env python3
"""MARSOUD-TKT-P1-TOKENS (Abdelhamid 2026-08-28) — brand primitives
must consume --ms-brand tokens, not literal hexes.

Design audit finding P0-01: _design_tokens.html declared `--ms-brand`
as canonical but no primitive in base.html or admin/base.html consumed
it. The token layer was decorative — editing it changed nothing on any
page. P1 rewired the brand-identity primitives (btn-primary,
gradient-heading, nav-link.active, body background) to reference
var(--ms-brand*) instead of hardcoded hexes; Path A also flipped the
token values so no visible change on tenant. Admin .btn-primary unified
navy → green in the same pass.

This audit is the regression net. Every check is a static file read +
substring assertion — no app bootstrap, no DB — so it runs in <1s and
can be gated on every commit. If a future ticket reintroduces a brand
hex literal inside one of the touched primitives, this audit fails
loudly instead of shipping silently.

Checks:
  1. _design_tokens.html declares the P1-required token values.
  2. Tenant base.html primitives reference var(--ms-brand*) and do NOT
     hardcode the brand hexes inside their rule blocks.
  3. Admin base.html .btn-primary references var(--ms-brand*), does NOT
     contain the retired navy gradient, and does NOT hardcode the
     brand hexes inline.
  4. Neither shell contains the retired `crimson:` Tailwind alias or
     any `from-crimson-`/`to-crimson-` call sites.
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
    substring checks like "does .btn-primary still hardcode #1849A9?" don't
    false-positive on retirement notes in the source ("was #1849A9, now
    via var(--ms-brand)"). The source of truth for these audits is live
    code, not documentation about what live code USED to look like.

    ORDER MATTERS. JS `//` comments must be stripped BEFORE CSS `/* … */`,
    because a JS comment can contain the two characters `/*` inside its
    text (e.g. Tailwind's `// admin/*.html templates …`), which would
    otherwise open a spurious CSS-comment match that greedy-runs to the
    next `*/` many lines away, silently eating live CSS in between."""
    # Jinja first — the safest to remove and has no overlap risk.
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    # JS `//` line comments next — end-of-line anchored.
    src = re.sub(r"//[^\n]*", "", src)
    # CSS block comments last — the greedy-risk regex only runs after
    # the `//` comments that could have introduced fake `/*` openers
    # have been removed.
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


def _rule_block(css, selector):
    """Return the concatenated bodies of EVERY rule whose selector list
    contains `selector` (matched as a whole token). Empty string if none.

    Concatenation matters because a class is often declared across two
    rules — e.g. admin/base.html has a compound
    `.btn, .btn-primary, .btn-secondary, .btn-danger { padding … }` PLUS
    a specific `.btn-primary { background … }`. Returning only the first
    match hides half the definition. We want to assert against the union.
    """
    parts = []
    for segment in css.split("}"):
        if "{" not in segment:
            continue
        head, body = segment.split("{", 1)
        # Match selector as a whole token: preceded by start-of-string,
        # whitespace, or comma; followed by whitespace, comma, `{`, or
        # `:` (so `.btn-primary` matches `.btn-primary:hover`'s selector
        # too — the hover block is part of "how .btn-primary looks").
        pattern = r"(?:^|[\s,])" + re.escape(selector) + r"(?=[\s,{:])"
        if re.search(pattern, head):
            parts.append(body)
    return "\n".join(parts)


@check("1. _design_tokens.html declares P1 brand + app-bg tokens")
def _():
    src = _read("app/templates/_design_tokens.html")
    required = {
        "--ms-brand:":       "#059669",
        "--ms-brand-dark:":  "#047857",
        "--ms-brand-mid:":   "#10b981",
        "--ms-brand-tint:":  "#ECFDF5",
    }
    misses = []
    for token, value in required.items():
        # Line-level match: token declaration must set exactly this value.
        pattern = re.escape(token) + r"\s+" + re.escape(value)
        if not re.search(pattern, src, re.IGNORECASE):
            misses.append(f"{token} {value}")
    assert not misses, \
        f"missing/wrong token declarations: {misses}"
    assert "--ms-app-bg:" in src and "linear-gradient(180deg" in src, \
        "--ms-app-bg not declared as a gradient token"
    return "brand ramp + app-bg all declared"


@check("2. tenant base.html primitives consume --ms-brand tokens")
def _():
    src = _strip_comments(_read("app/templates/base.html"))

    body_block = _rule_block(src, "body")
    assert "var(--ms-app-bg)" in body_block, \
        "body { background: … } does not reference --ms-app-bg"
    assert "#F0F7FF" not in body_block, \
        "body background still has the literal #F0F7FF gradient stop; " \
        "should be tokenised via --ms-app-bg"

    for selector, must_contain in [
        (".btn-primary",   "var(--ms-brand-dark)"),
        (".btn-primary",   "var(--ms-brand)"),
        (".nav-link.active", "var(--ms-brand)"),
        (".gradient-heading", "var(--ms-brand)"),
    ]:
        block = _rule_block(src, selector)
        assert block, f"{selector} rule not found in base.html"
        assert must_contain in block, \
            f"{selector} does not reference {must_contain}"

    # And the brand literals must no longer appear inside those blocks.
    # Whitelist: .gradient-heading's darker origin (#065F46) stays inline
    # by design (no matching --ms-brand-darker token today).
    for selector, forbidden in [
        (".btn-primary",     ["#047857", "#059669"]),
        (".nav-link.active", ["#059669", "#ECFDF5"]),
        # NOTE: .gradient-heading is allowed to keep #065F46 — that's
        # not in --ms-brand-dark (#047857). But #059669 must be gone.
        (".gradient-heading", ["#059669"]),
    ]:
        block = _rule_block(src, selector)
        leaks = [h for h in forbidden if h.lower() in block.lower()]
        assert not leaks, \
            f"{selector} still hardcodes {leaks} inline — tokenise via " \
            "--ms-brand / --ms-brand-dark / --ms-brand-tint"
    return "body + .btn-primary + .nav-link.active + .gradient-heading all via tokens"


@check("3. admin/base.html .btn-primary unified via tokens (green, not navy)")
def _():
    src = _strip_comments(_read("app/templates/admin/base.html"))

    body_block = _rule_block(src, "body")
    assert "var(--ms-app-bg)" in body_block, \
        "admin body does not reference --ms-app-bg"

    btn_block = _rule_block(src, ".btn-primary")
    assert btn_block, "admin .btn-primary rule not found"
    assert "var(--ms-brand)" in btn_block, \
        "admin .btn-primary does not reference var(--ms-brand)"
    assert "var(--ms-brand-dark)" in btn_block, \
        "admin .btn-primary does not reference var(--ms-brand-dark)"
    # Retired navy gradient must be gone from the rule block AND from
    # anywhere else in the file (the whole point of unification).
    for retired in ("#1849A9", "#0C2461"):
        assert retired not in src, \
            f"retired navy literal {retired} still present in admin/base.html"
    # Brand hexes must not have leaked back in inside .btn-primary.
    for leak in ("#047857", "#059669"):
        assert leak.lower() not in btn_block.lower(), \
            f"admin .btn-primary still hardcodes {leak}; use var(--ms-brand*)"

    nav_block = _rule_block(src, ".nav-link.active")
    assert "var(--ms-brand)" in nav_block, \
        "admin .nav-link.active does not reference var(--ms-brand)"
    return "admin .btn-primary + .nav-link.active + body all via tokens; navy retired"


@check("4. `crimson` alias fully retired from both shells")
def _():
    for path in ("app/templates/base.html", "app/templates/admin/base.html"):
        src = _strip_comments(_read(path))
        # The Tailwind alias DECLARATION — `crimson: { … }` inside the
        # theme.extend.colors object. Match only the declaration form,
        # not any mention of the word inside a comment (my own
        # retirement note documents the fact `crimson:` used to exist,
        # so a naive substring check would false-positive on it).
        assert not re.search(r"^\s*crimson\s*:\s*\{", src, re.MULTILINE), \
            f"{path} still declares `crimson: { {} }` Tailwind alias — retire it"
        # Any lingering call site.
        for pattern in ("from-crimson-", "to-crimson-",
                        "bg-crimson-", "text-crimson-",
                        "border-crimson-"):
            assert pattern not in src, \
                f"{path} still uses `{pattern}…` class"
    return "no crimson alias or call sites in either shell"


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
