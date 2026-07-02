#!/usr/bin/env python3
"""MARSOUD-TZ-01 (pass 2) — bare-date strftime on datetime fields.

Pass 1 converted any strftime with %H/%I/%M in the format string.
Pass 2 catches the remaining case: a `strftime('%Y-%m-%d')` on a
datetime column, which STILL suffers day-boundary drift near midnight
UTC vs the company's local zone.

We identify "datetime-like" values by name suffix (`_at`, `next_meeting`,
etc.). Bare date columns (`issue_date`, `due_date`, `report_date`) are
skipped because a Date value is already timezone-agnostic.

Prints a preview; run with --apply to write.
"""
import argparse
import re
from pathlib import Path


TEMPLATE_DIR = Path("app/templates")

DATETIME_SUFFIXES = (
    "_at",              # created_at, updated_at, paused_at, reactivated_at, ...
    "next_meeting",     # Lead.next_meeting (datetime, not date)
    "converted_at",     # explicit — matches _at fall-through too
    "activity_date",    # LeadActivity.activity_date (DateTime)
    "expires",          # SubscriptionReminderSent.expires + expires_at
)

# Match: EXPR.strftime('<no %H, %I, or %M in format>')
# strftime format codes are case-sensitive: uppercase %H/%I/%M are
# hours/minutes; lowercase %h/%i/%m are month/etc — leave those alone.
STRF_RE = re.compile(
    r"(?P<expr>[A-Za-z_][\w\.]*)\.strftime"
    r"\(\s*(?P<quote>['\"])"
    r"(?P<fmt>[^'\"]*?)"
    r"(?P=quote)\s*\)"
)


def _fmt_has_time(fmt):
    """True if the format string contains an hour/minute component."""
    # Walk %-tokens looking for H/I/M (uppercase only — the time chars).
    i = 0
    while i < len(fmt):
        if fmt[i] == "%" and i + 1 < len(fmt):
            if fmt[i + 1] in ("H", "I", "M"):
                return True
            i += 2
        else:
            i += 1
    return False


def _expr_is_datetime_like(expr):
    """True if the dotted expression ends in a known datetime attribute."""
    tail = expr.rsplit(".", 1)[-1]
    return any(tail.endswith(sfx) for sfx in DATETIME_SUFFIXES)


def _line_is_input_value(line):
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
                fmt = m.group("fmt")
                # Skip if pass-1 already handled it (has %H/%I/%M).
                if _fmt_has_time(fmt):
                    return m.group(0)
                if not _expr_is_datetime_like(expr):
                    return m.group(0)   # leave date-like fields alone
                quote = m.group("quote")
                return f"{expr} | company_dt({quote}{fmt}{quote})"

            new_line = STRF_RE.sub(_swap, line)
            if new_line != line:
                touched = True
                changes += new_line.count("company_dt") - line.count("company_dt")
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
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sweep(apply=args.apply)
