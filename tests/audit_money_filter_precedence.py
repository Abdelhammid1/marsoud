#!/usr/bin/env python3
"""MARSOUD-REPORTS-MONEY-01 — Jinja filter precedence lint.

In Jinja, the `|` filter binds tighter than `+`/`-`/`*`/`/`.  That
means `{{ a + b|money }}` parses as `a + money(b)`, NOT
`money(a + b)`.  With `money` returning a string, the outer `+`
raises TypeError at render time — the page crashes.

The MARSOUD-REPORTS-MONEY-01 mass rewrite (`"%.2f"|format(x)` →
`x|money`) inadvertently produced 9 such sites — including
`pdfs/invoice.html:262` inside the invoice line loop, where every
invoice PDF would 500 on render.  All nine were parenthesized in
the follow-up commit.

This audit codifies the invariant so a future rewrite (or a hand
edit) can't reintroduce the bug: walk every `.html` template and
fail if any `<identifier-or-expr> [+*-/] <expr>|money` (or the
same shape ending in `|amount_ar`) survives without an outer
paren wrap.

Runs standalone — no Flask app context needed.  Fast enough to
include in the deploy-gate audit sweep.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "app" / "templates"

# Match `{{ ... }}` blocks that contain `A op B|filter` where the
# filter is `money` or `amount_ar` and no outer paren wraps the sum.
# Approach: scan every `{{ ... }}` expression, then look for
# `X <op> Y|filter` — but only fail if X and Y are NOT already
# inside a common paren pair.
#
# Practical implementation: reject any occurrence of a character
# that is a variable-name terminator followed by whitespace + `+`
# / `-` / `*` / `/` followed by another identifier chain that
# ends `|money` (or `|amount_ar`).  The regex requires no `(` on
# the whole run between the operator and the filter — so a
# properly-wrapped `(a + b)|money` is skipped (the `(` breaks the
# left side out of the operator's LHS).
BAD_MONEY = re.compile(
    r"""
    \{\{\s*                       # start of Jinja expression
    (?P<body>
      [^{}()]{0,150}?             # LHS: anything without brackets/parens
      [\w\]\)]                    # identifier char before the operator
      \s* [-+*/] \s*              # arithmetic operator
      [\w\.(][^{}()]{0,150}?      # RHS starts with an identifier or `(`
      \| \s* (?:money|amount_ar) \b
    )
    """,
    re.VERBOSE,
)


def scan():
    hits = []
    for p in TEMPLATES.rglob("*.html"):
        text = p.read_text(encoding="utf-8")
        for m in BAD_MONEY.finditer(text):
            # Locate the line number for the report.
            line_no = text.count("\n", 0, m.start()) + 1
            hits.append((p.relative_to(ROOT).as_posix(), line_no,
                          m.group("body").strip()))
    return hits


def main():
    hits = scan()
    if not hits:
        print("PASS  no `A op B|money` (unparenthesised) sites remain")
        print()
        print("----  1 passed, 0 failed  ----")
        return 0
    print(f"FAIL  {len(hits)} unparenthesised precedence bug(s):")
    for path, line, snippet in hits:
        # Truncate long snippets for readability
        s = snippet if len(snippet) < 100 else snippet[:97] + "..."
        print(f"        {path}:{line}  =>  {s}")
    print()
    print(f"----  0 passed, {len(hits)} failed  ----")
    return 1


if __name__ == "__main__":
    sys.exit(main())
