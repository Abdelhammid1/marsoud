#!/usr/bin/env python3
"""MARSOUD-TZ-01 — one-off sweep script.

Replaces `.strftime('...%H...%M...')` on Jinja values with
`| company_dt('...%H...%M...')` — but only when:
  1. The format contains an hour component (%H, %I) — proving it's a
     datetime, not a naked date. This makes the swap safe: a bare
     %Y-%m-%d strftime on a date field can never differ across
     timezones, so we leave it alone.
  2. The strftime isn't inside a `value="…"` attribute (those go back
     through form submission and must round-trip unchanged).

Prints a diff summary; run with --apply to write changes."""
import argparse
import re
from pathlib import Path


TEMPLATE_DIR = Path("app/templates")

# Match: SOMEEXPR.strftime('<format-with-%H-or-%I>')
# where SOMEEXPR is a chain of identifiers and dots (e.g. r.created_at).
# Captures: [pre, expr, quote, fmt, post]
STRF_RE = re.compile(
    r"(?P<expr>[A-Za-z_][\w\.]*)\.strftime"
    r"\(\s*(?P<quote>['\"])(?P<fmt>[^'\"]*[HIhi][^'\"]*)(?P=quote)\s*\)"
)


def _line_is_input_value(line):
    """True if the line has `value=` immediately preceding the match."""
    # Cheap heuristic — if the whole line has `value="` earlier, skip it.
    return 'value="' in line or "value='" in line


def sweep(apply=False):
    changes = 0
    files_touched = []
    for tpl in TEMPLATE_DIR.rglob("*.html"):
        if ".bak" in tpl.name:
            continue
        text = tpl.read_text(encoding="utf-8")
        new_lines = []
        touched = False
        for line in text.splitlines(keepends=True):
            if not STRF_RE.search(line):
                new_lines.append(line)
                continue
            if _line_is_input_value(line):
                new_lines.append(line)
                continue

            def _swap(m):
                expr = m.group("expr")
                quote = m.group("quote")
                fmt = m.group("fmt")
                return f"{expr} | company_dt({quote}{fmt}{quote})"

            new_line = STRF_RE.sub(_swap, line)
            if new_line != line:
                touched = True
                changes += 1
            new_lines.append(new_line)
        if touched:
            files_touched.append(str(tpl))
            if apply:
                tpl.write_text("".join(new_lines), encoding="utf-8")
    print(f"changes={changes}  files_touched={len(files_touched)}")
    for f in files_touched:
        print(f"  {f}")
    return changes, files_touched


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                          help="Actually write the changes.")
    args = parser.parse_args()
    sweep(apply=args.apply)
