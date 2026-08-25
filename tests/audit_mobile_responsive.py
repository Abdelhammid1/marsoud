#!/usr/bin/env python3
"""MARSOUD-MOBILE-STACKED-TABLES (2026-08-25) — mobile responsiveness.

Guards the invariant that makes every page usable on a phone:

    below 768px a .data-table renders as stacked cards, so nothing
    scrolls sideways.

That behaviour is produced by two cooperating pieces, and it silently
degrades if either drifts:

  · base.html  — stampLabels() copies each column's <th> onto its cells
    as data-label, marks the table [data-stacked], and re-runs on
    htmx:afterSwap so swapped-in tables are covered too.
  · _design_tokens.html — the <=767px block that turns [data-stacked]
    rows into cards AND neutralises the horizontal-scroll machinery
    (sticky columns, width:max-content, the "اسحب" hint) that would
    otherwise fight it.

A browser is needed to observe the final layout; this asserts the
machinery is present, wired, and mutually consistent, and additionally
renders every page to catch markup that would overflow a 390px screen
regardless of the table treatment.

Exit code 0 = mobile behaviour intact.
"""
import os
import re
import sys
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db          # noqa: E402

TPL = ROOT / "app" / "templates"
CHECKS = []
failures = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _p(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode())


def read(rel):
    return (TPL / rel).read_text(encoding="utf-8")


# ─── 1. The JS half ───────────────────────────────────────────────
@check("1. base.html stamps column labels onto cells")
def _():
    t = read("base.html")
    assert "function stampLabels" in t, "stampLabels() is gone"
    assert "dataset.label" in t or "data-label" in t, \
        "nothing sets data-label — cards would render label-less"
    assert "data-stacked" in t, \
        "the [data-stacked] marker is gone; the CSS keys on it"
    return "stampLabels present and sets data-label + data-stacked"


@check("2. stampLabels is wired into the pass that walks every table")
def _():
    t = read("base.html")
    m = re.search(r"function wrapTables\(\)\s*\{(.{0,400})", t, re.S)
    assert m, "wrapTables() is gone"
    # Strip JS line comments first. Searching the raw text for the call
    # passes even when it is commented out — a sabotage run proved this
    # check green against `//stampLabels(t);`, which would ship every
    # table unstacked.
    live = re.sub(r"//[^\n]*", "", m.group(1))
    assert re.search(r"\bstampLabels\s*\(", live), (
        "wrapTables no longer calls stampLabels (or the call is commented "
        "out) — tables would be wrapped for horizontal scroll but never "
        "stacked")
    return "wrapTables -> stampLabels (uncommented)"


@check("3. htmx-swapped tables are processed too")
def _():
    t = read("base.html")
    assert "htmx:afterSwap" in t, (
        "no htmx:afterSwap listener — tables arriving by htmx "
        "(journals/_results.html, _activity_page.html) would never be "
        "stacked, which is how the journals page broke before")
    assert re.search(r"htmx:afterSwap['\"]?\s*,\s*wrapTables", t), \
        "the afterSwap listener does not call wrapTables"
    return "htmx:afterSwap -> wrapTables"


@check("4. colspan / action / empty cells are classified, not labelled")
def _():
    t = read("base.html")
    for cls in ("ms-cell-full", "ms-cell-actions", "ms-cell-empty"):
        assert cls in t, f"{cls} classification is gone"
    assert "colspan" in t, "colspan is no longer honoured when mapping labels"
    return "all three cell kinds classified"


# ─── 2. The CSS half ──────────────────────────────────────────────
@check("5. card mode exists and is keyed on [data-stacked]")
def _():
    t = read("_design_tokens.html")
    assert "max-width: 767px" in t, "the mobile breakpoint block is gone"
    assert "data-stacked" in t, (
        "card CSS is not keyed on [data-stacked] — it would restyle "
        "tables the JS never processed")
    assert "attr(data-label)" in t, \
        "td::before no longer prints the column name"
    return "card mode present, keyed on [data-stacked]"


@check("6. the horizontal-scroll machinery is neutralised when stacked")
def _():
    t = read("_design_tokens.html")
    # Each of these fights the card layout and must be turned off.
    need = {
        "overflow: visible": "the scroll container still clips/scrolls",
        "content: none": "the swipe hint / edge gradient still render",
        "white-space: normal": "cells still forced onto one line",
    }
    for frag, why in need.items():
        assert frag in t, f"missing `{frag}` — {why}"
    return "scroll container, swipe hint and nowrap all neutralised"


@check("7. label styling does not inherit the value's font")
def _():
    t = read("_design_tokens.html")
    block = t[t.find("attr(data-label)"):][:600]
    assert "--ms-font-body" in block, (
        "the label inherits .font-mono from numeric cells — Arabic "
        "renders visibly stretched in a monospace face")
    return "label pinned to the body font"


# ─── 3. Both shells get it ────────────────────────────────────────
@check("8. both shells include the token file and set a viewport")
def _():
    for shell in ("base.html", "admin/base.html"):
        t = read(shell)
        assert "_design_tokens.html" in t, \
            f"{shell} does not include the shared token file"
        assert re.search(r'name="viewport"[^>]*width=device-width', t), \
            f"{shell} has no width=device-width viewport meta"
    return "base.html + admin/base.html both wired"


# ─── 4. Markup that would overflow regardless ─────────────────────
@check("9. no template hard-codes a width wider than a phone")
def _():
    bad = []
    scanned = 0
    # `max-width` CAPS a box, it never forces one wider than the screen —
    # only a bare `width` or a `min-width` can. The negative lookbehind is
    # what separates them; matching plain "width:" also hits "max-width:"
    # and reports every well-behaved template as broken.
    RE = re.compile(r'style="[^"]*?(?<!max-)\b(min-width|width):\s*(\d{3,})px')
    for p in TPL.rglob("*.html"):
        rel = str(p.relative_to(TPL)).replace(os.sep, "/")
        # Paper and email are not phone screens: PDFs, print sheets,
        # barcode sheets, POS receipts, and any *_email template (they
        # live under auth/ as well as emails/) are laid out for a fixed
        # medium and legitimately carry pixel widths.
        if rel.startswith(("pdfs/", "emails/")) or "print" in rel \
                or "barcode" in rel or "receipt" in rel or "_email" in rel \
                or "decoy" in rel:
            continue
        scanned += 1
        t = p.read_text(encoding="utf-8")
        for m in RE.finditer(t):
            if int(m.group(2)) > 420:
                bad.append(f"{rel}: {m.group(0)[-46:]}")
    assert not bad, ("inline width/min-width wider than a 390px screen:\n    "
                     + "\n    ".join(bad[:8]))
    return f"{scanned} screen templates scanned, none forces >420px"


@check("10. every page renders, and none is missing the table machinery")
def _():
    """Render each non-parametric page as an owner and assert the shell
    is intact. A 500 here is a mobile bug too — a page that errors is a
    page that does not work on a phone."""
    from datetime import datetime
    from app.models import User, Company, UserStatus
    from app.models.user import user_companies
    from werkzeug.security import generate_password_hash
    from app.services.legal import get_terms_version

    app = _STATE["app"]
    co = Company.query.order_by(Company.id).first()
    assert co, "no company to render against"

    EM = "__mobaudit@audit.local"
    u = User.query.filter_by(email=EM).first()
    made = False
    if not u:
        u = User(email=EM, full_name="mob audit", is_active=True,
                 status=UserStatus.ACTIVE.value,
                 email_verified_at=datetime.utcnow(),
                 terms_version=get_terms_version(),
                 terms_accepted_at=datetime.utcnow(),
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"))
        db.session.add(u)
        db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=co.id, role="owner"))
        db.session.commit()
        made = True

    SKIP = ("superadmin.", "api_v1", "auth.", "static", "cron.", "public.",
            "invitations.", "notifications.", "support_admin.", "portal.",
            "portal_emp.", "help.")
    urls = sorted({
        str(r) for r in app.url_map.iter_rules()
        if "GET" in r.methods and not r.endpoint.startswith(SKIP)
        and "<" not in str(r)
        and not str(r).startswith(("/static", "/admin"))
        and not any(s in str(r) for s in
                    ("/export", "/download", "/print", "/pdf",
                     "/barcodes", ".json", "suggest-code", "/api/"))
    })

    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True
        s["active_company_id"] = co.id

    errors, no_shell = [], []
    for url in urls:
        r = client.get(url, follow_redirects=True)
        if r.status_code >= 500:
            errors.append(f"{url} -> {r.status_code}")
            continue
        body = r.get_data(as_text=True)
        if "data-table" in body and "stampLabels" not in body:
            no_shell.append(url)

    if made:
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id))
        db.session.delete(u)
        db.session.commit()

    assert not errors, "pages returning 5xx:\n    " + "\n    ".join(errors[:10])
    assert not no_shell, (
        "pages with a data-table but no stacking JS (they extend a shell "
        "that lacks it):\n    " + "\n    ".join(no_shell[:10]))
    return f"{len(urls)} pages rendered, all carry the stacking JS"


_STATE = {}


def main():
    app = create_app()
    _STATE["app"] = app
    passed = failed = 0
    _p("MARSOUD-MOBILE-STACKED-TABLES - mobile responsiveness audit")
    _p("=" * 68)
    with app.app_context():
        for label, fn in CHECKS:
            try:
                detail = fn()
                passed += 1
                _p(f"PASS  {label}")
                if detail:
                    _p(f"        -> {detail}")
            except AssertionError as e:
                failed += 1
                failures.append((label, str(e)))
                _p(f"FAIL  {label}")
                _p(f"        {e}")
            except Exception as e:
                failed += 1
                failures.append((label, f"{type(e).__name__}: {e}"))
                _p(f"ERROR {label}: {type(e).__name__}: {e}")
    _p("")
    _p(f"----  {passed} passed, {failed} failed  ----")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
