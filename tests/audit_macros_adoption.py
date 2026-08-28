#!/usr/bin/env python3
"""MARSOUD-TKT-P0-05-MACROS (Abdelhamid 2026-08-28) — the shared UI macros
must stay adopted in the touched templates.

Design audit 2026-08-28 finding P0-05: `app/templates/_macros.html` shipped
`empty_state`, `stat_tile`, `status_badge`, and `page_header` on
2026-08-18 to end 200+ inline reimplementations across 273 templates —
but zero templates ever imported it. Every list page rolled its own
empty state, every stat row rolled its own tile, every invoice + bill
page duplicated the ~40-line status-class + Arabic-label map.

This ticket adopted the macros in 15 highest-visibility templates as the
ratchet foundation. Future work follows the rule 'touched = migrated'.

This audit is the regression net. Every check is a static file read +
substring assertion (no DB, no app bootstrap, <1s). It fails if a
touched template loses its `_macros.html` import or if the retired
inline patterns (STATUS_CLASSES dict, LABELS_AR dict, raw enum badge)
creep back into the four status_badge-migrated files. Prints adoption %
so future ratchet growth is visible without extra tooling.

Explicitly OUT of scope (documented at check 4): `admin/*.html` (has its
own class family), `portal_emp/*` (own density), detail pages
(`*/detail.html`, `*/view.html`, except the two invoice/bill views),
per-column kanban `فارغ` markers (too tall for narrow columns), emails,
PDFs, auth flow, error pages, help/public content pages, and the shells
themselves.
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


# Templates migrated in this branch — the ratchet's first-wave set.
# Ordered by migration order so a failure trace reads chronologically.
RATCHETED = [
    "invoices/index.html",
    "vendor_bills/index.html",
    "invoices/view.html",
    "vendor_bills/view.html",
    "customers/index.html",
    "products/index.html",
    "vendors/index.html",
    "payroll/index.html",
    "notifications/index.html",
    "inventory/index.html",
    "hr/index.html",
    "dashboard/index.html",
    "projects/index.html",
    "tasks/index.html",
    "leads/index.html",
]

# Subset that had a duplicated status-class + Arabic-label map inline;
# the macro is the whole point for these four.
STATUS_BADGE_MIGRATED = [
    "invoices/index.html",
    "vendor_bills/index.html",
    "invoices/view.html",
    "vendor_bills/view.html",
]


def _read(relpath):
    return (TPL / relpath).read_text(encoding="utf-8")


def _strip_comments(src):
    """Remove Jinja `{# … #}`, JS `//` line, and CSS `/* … */` comments.
    Same idiom as audit_brand_tokens_consumed.py / audit_a11y_baseline.py
    / audit_email_hero_unified.py — retirement-doc notes in the source
    (documenting `was {% set STATUS_CLASSES = {…} %}`) must not
    false-positive the check. Strip order: Jinja → JS → CSS."""
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


@check("1. every ratcheted template imports _macros.html as ui")
def _():
    misses = []
    for rel in RATCHETED:
        src = _read(rel)
        # Two accepted forms: `{% import '_macros.html' as ui %}` or
        # `{% from '_macros.html' import empty_state, … %}`. First form
        # is the house convention (all 15 migrations use it), but the
        # check accepts either so a future template can vary style.
        if not re.search(r"\{%\s*(import|from)\s+['\"]_macros\.html['\"]", src):
            misses.append(rel)
    assert not misses, \
        f"{len(misses)} ratcheted template(s) missing macro import: {misses}"
    return f"all {len(RATCHETED)} ratcheted templates carry the import"


@check("2. status_badge-migrated templates do NOT re-inline the dicts")
def _():
    """The whole point of migrating invoices/{index,view} +
    vendor_bills/{index,view} was to delete the duplicated
    STATUS_CLASSES + STATUS_LABELS_AR dicts. If any of them creeps
    back in a future touch, the macro's single source of truth is
    silently forked again."""
    misses = []
    for rel in STATUS_BADGE_MIGRATED:
        src = _strip_comments(_read(rel))
        # Regex on the whole `{% set NAME = { … enum-code → class … } %}`
        # shape. Match the specific dict names the old templates used.
        forbidden_names = ("STATUS_CLASSES", "STATUS_LABELS_AR",
                           "status_class", " sc ", " sl ")
        # `status_class` matches too broadly (also `status_class_map`),
        # so we scope it to a `{% set` prefix.
        for name in ("STATUS_CLASSES", "STATUS_LABELS_AR", "status_class"):
            if re.search(r"\{%\s*set\s+" + name + r"\s*=", src):
                misses.append(f"{rel} → {{% set {name} = ... %}}")
        # And the compact `{% set sc = { 'DRAFT': …} %}` / `sl` forms.
        for name in ("sc", "sl"):
            if re.search(r"\{%\s*set\s+" + name + r"\s*=\s*\{\s*['\"]DRAFT",
                          src):
                misses.append(f"{rel} → {{% set {name} = {{'DRAFT': …}} %}}")
        # And a raw <span class="badge {{ status_class.get(...) }}">
        # in case someone kept the dict but renamed it and hand-wrote
        # the span; the macro must be the way in.
        if re.search(r'<span[^>]*class="badge\s+\{\{[^}]*status_class', src):
            misses.append(f"{rel} → raw status_class-driven <span> re-appeared")
    assert not misses, \
        f"retired inline status-badge patterns re-appeared:\n  " \
        + "\n  ".join(misses)
    return "no retired STATUS_CLASSES/LABELS_AR dicts in the 4 badge-migrated files"


@check("3. adoption ratio across app/templates/")
def _():
    """Informational — reports what percent of page templates now import
    _macros.html. Does not fail the audit; the ratchet grows over time
    as touched templates migrate. Baseline this ticket lands: 15 / ~= N."""
    imports = 0
    total_pages = 0
    for path in sorted(TPL.rglob("*.html")):
        rel = path.relative_to(TPL).as_posix()
        # Only count "page" templates: skip partials, PDFs, emails,
        # print views, the shells, and error/help/public/auth pages.
        if rel.startswith(("emails/", "pdfs/", "party_ledger/print",
                           "auth/", "errors/", "help/", "public/",
                           "invitations/", "portal_emp/", "admin/",
                           "_", "base.html", "admin/base.html")):
            continue
        if path.name.startswith("_"):
            continue
        total_pages += 1
        if re.search(r"\{%\s*(import|from)\s+['\"]_macros\.html['\"]",
                      path.read_text(encoding="utf-8")):
            imports += 1
    pct = (imports / total_pages * 100) if total_pages else 0
    # Sanity: the 15 ratcheted templates all fall inside the "page"
    # scope above, so this ratio must be at least 15/total.
    assert imports >= len(RATCHETED), \
        f"adoption regressed: expected ≥{len(RATCHETED)} imports " \
        f"(this branch's ratchet), got {imports}"
    return f"adoption: {imports}/{total_pages} ({pct:.1f}%) — ratchet in place"


@check("4. shells do NOT import _macros.html (macros are page-level only)")
def _():
    """The macros expand to CSS classes that _design_tokens.html defines.
    Importing them into the shell (base.html / admin/base.html) would
    create a circular include (tokens → shell → macros → tokens) and
    is architecturally wrong: shells provide the chrome, pages provide
    the content. If a shell ever needs a stat_tile, it's a signal to
    extract a new shell-scoped primitive, not to blur the layers."""
    for shell in ("base.html", "admin/base.html"):
        src = _read(shell)
        assert not re.search(
            r"\{%\s*(import|from)\s+['\"]_macros\.html['\"]", src
        ), f"{shell} imports _macros.html — shells should stay pure chrome"
    return "both shells stay chrome-only"


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
