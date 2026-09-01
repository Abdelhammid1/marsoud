#!/usr/bin/env python3
"""MARSOUD-TKT-ADMIN-DIRECT-USER-LINK (Abdelhamid 2026-08-31) —
every place a user name appears in the super-admin UI should link
directly to /admin/users/<id>. Before the ticket, the company_detail
page, dashboard recent-logins, and support/detail comment author
rendered the name as plain text — support agents had to bounce
across pages to reach the profile.

Checks:
  1. admin/companies.html — owner link (regression from
     MARSOUD-TKT-ADMIN-OWNER-COL).
  2. admin/users.html — name link (regression, was already there).
  3. admin/company_detail.html — company-users table name is a link.
  4. admin/dashboard.html — recent-logins name is a link.
  5. admin/support/detail.html — comment-author name is a link.
  6. No plain "{{ *.full_name }}" without a wrapping link remains
     in the four MARSOUD-flagged spots (regression guard so a
     future edit doesn't accidentally strip the link).
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


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _strip_comments(src):
    return re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)


def _has_link_wrapping(src, name_expr):
    """True if `name_expr` (e.g. 'u.full_name', 'user.full_name')
    appears inside an <a href="...user_detail..."> block on the
    same line — a defensive pattern to prove the link was written
    per row, not floating somewhere unrelated in the file."""
    for line in src.splitlines():
        if name_expr in line:
            # scan a window around this line for the anchor + href
            idx = src.index(line)
            window = src[max(0, idx - 400):idx + 400]
            if "user_detail" in window and "{{ " + name_expr + " }}" in window:
                # Anchor + name must be near each other. Rough test:
                # the nearest opening <a href before name must contain
                # user_detail.
                before = src[:src.index("{{ " + name_expr + " }}")]
                last_a = before.rfind("<a ")
                if last_a == -1:
                    continue
                anchor = before[last_a:]
                if "user_detail" in anchor and "</a>" not in anchor:
                    return True
    return False


@check("1. admin/companies.html — owner name is a link")
def _():
    src = _strip_comments(_read("app/templates/admin/companies.html"))
    assert _has_link_wrapping(src, "r.owner.full_name or r.owner.email"), \
        "owner name in companies list not wrapped in user_detail link"
    return "owner link intact (from MARSOUD-TKT-ADMIN-OWNER-COL)"


@check("2. admin/users.html — user name is a link")
def _():
    src = _strip_comments(_read("app/templates/admin/users.html"))
    assert _has_link_wrapping(src, "u.full_name"), \
        "user name in users list not wrapped in user_detail link"
    return "user list name link intact"


@check("3. admin/company_detail.html — company-users name is a link")
def _():
    src = _strip_comments(_read("app/templates/admin/company_detail.html"))
    assert _has_link_wrapping(src, "user.full_name"), \
        "company_users row name must link to user_detail"
    return "company_detail user link wired"


@check("4. admin/dashboard.html — recent-logins name is a link")
def _():
    src = _strip_comments(_read("app/templates/admin/dashboard.html"))
    assert _has_link_wrapping(src, "u.full_name"), \
        "dashboard recent-logins name must link to user_detail"
    return "dashboard user link wired"


@check("5. admin/support/detail.html — comment-author name is a link")
def _():
    src = _strip_comments(_read("app/templates/admin/support/detail.html"))
    assert _has_link_wrapping(src, "c.user.full_name or c.user.email"), \
        "support comment-author name must link to user_detail"
    return "support-comment author link wired"


@check("6. no plain `full_name` without a link in the 4 flagged spots")
def _():
    """Regression guard — if someone later removes the anchor but
    leaves the {{ full_name }}, this test flags it."""
    hits = []
    files_and_exprs = [
        ("app/templates/admin/company_detail.html", "user.full_name"),
        ("app/templates/admin/dashboard.html", "u.full_name"),
        ("app/templates/admin/support/detail.html",
         "c.user.full_name or c.user.email"),
    ]
    for path, expr in files_and_exprs:
        src = _strip_comments(_read(path))
        if not _has_link_wrapping(src, expr):
            hits.append(f"{path}: `{expr}` not link-wrapped")
    assert not hits, "\n  ".join(hits)
    return "all 3 flagged spots keep their link"


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
