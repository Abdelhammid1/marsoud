#!/usr/bin/env python3
"""MARSOUD-TKT-PDFS-06-FINAL (Abdelhamid 2026-08-29) — the "16 of 16"
contract: every PDF template in the app carries the shared brand
markers.

Wraps up MARSOUD-TKT-PDFS-01→05. Six branches migrated 15 of 16 PDFs
to WeasyPrint templates extending pdfs/_shell.html. This ticket
retrofitted the last one (party_ledger/print.html) — it doesn't
extend the shell (multi-page Chromium render, different constraints)
but carries the same palette + brand row + corner accents.

This audit is the whole-system contract. If a NEW PDF template is
added anywhere in `app/templates/pdfs/*.html` OR in
`app/templates/party_ledger/print.html`, it MUST:
  - Declare the brand green (#059669).
  - Declare the navy secondary (#0A2540).
  - Use the Amiri font family.
  - Reference the Amiri @font-face (via {{ amiri_font_face|safe }} OR
    inline @font-face { font-family: 'Amiri').

And EVERY export_*_pdf service function in services/export.py that
has a `_legacy` fallback body MUST also call _weasyprint_render as
its primary path — the "WeasyPrint is the primary renderer" contract.

Checks:
  1. Every PDF template carries the brand green #059669.
  2. Every PDF template carries the navy #0A2540.
  3. Every PDF template declares 'Amiri' font-family + has Amiri
     font available (macro injection or inline @font-face).
  4. Every service with a `_legacy` fallback also uses
     _weasyprint_render as its primary path.
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TPL = ROOT / "app" / "templates"


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _read(relpath):
    return (ROOT / relpath).read_text(encoding="utf-8")


def _strip_comments(src):
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


def _consumer_pdf_templates():
    """Every PDF-consumer template in the app: everything under
    `pdfs/*.html` except partials (`_shell.html`, `_report_macros.html`)
    plus `party_ledger/print.html`."""
    out = []
    for p in sorted((TPL / "pdfs").glob("*.html")):
        if p.name.startswith("_"):
            continue
        out.append(p.relative_to(ROOT).as_posix())
    out.append("app/templates/party_ledger/print.html")
    return out


@check("1. every PDF template carries the brand green #059669")
def _():
    misses = []
    templates = _consumer_pdf_templates()
    for rel in templates:
        # Strip comments — retirement-doc notes referencing old palette
        # shouldn't count. We want the LIVE code to declare the color.
        src = _strip_comments(_read(rel))
        # Extending a shell? The shell carries the color, so the child
        # template inherits it — accept if the child declares `extends
        # "pdfs/_shell.html"`.
        if 'extends "pdfs/_shell.html"' in src:
            continue
        if "#059669" not in src:
            misses.append(rel)
    assert not misses, \
        "PDF templates missing brand green #059669:\n  " + "\n  ".join(misses)
    return f"all {len(templates)} PDF templates carry the brand green"


@check("2. every PDF template carries the navy secondary #0A2540")
def _():
    misses = []
    templates = _consumer_pdf_templates()
    for rel in templates:
        src = _strip_comments(_read(rel))
        if 'extends "pdfs/_shell.html"' in src:
            continue  # inherited from shell
        # Accept either #0A2540 or the lowercase #0a2540 (party_ledger
        # historically used lowercase; retrofit unified to uppercase but
        # the audit stays tolerant).
        if "#0A2540" not in src and "#0a2540" not in src:
            misses.append(rel)
    assert not misses, \
        "PDF templates missing navy #0A2540:\n  " + "\n  ".join(misses)
    return f"all {len(templates)} PDF templates carry the navy secondary"


@check("3. every PDF template uses Amiri + has font-face available")
def _():
    misses = []
    templates = _consumer_pdf_templates()
    for rel in templates:
        src = _strip_comments(_read(rel))
        extends_shell = 'extends "pdfs/_shell.html"' in src
        if extends_shell:
            # Shell declares font-family: 'Amiri' + injects the
            # font-face. Child inherits.
            continue
        # Standalone template — must declare Amiri family somewhere.
        assert "'Amiri'" in src or '"Amiri"' in src, \
            f"{rel} does not declare Amiri font-family"
        # And must reference the font-face injection macro OR inline
        # @font-face for Amiri. Either satisfies "Amiri is available".
        has_macro = "amiri_font_face" in src
        has_inline = re.search(
            r"@font-face\s*\{[^}]*font-family\s*:\s*['\"]Amiri['\"]",
            src, re.DOTALL,
        ) is not None
        if not (has_macro or has_inline):
            misses.append(f"{rel} (no macro AND no inline @font-face)")
    assert not misses, \
        "PDF templates without Amiri font available:\n  " + "\n  ".join(misses)
    return f"all {len(templates)} templates carry Amiri or inherit from shell"


@check("4. every legacy-fallback service also uses WeasyPrint as primary")
def _():
    """The 'WeasyPrint is the primary renderer' contract. For every
    _export_*_pdf_legacy function definition, there MUST also be a
    matching public export_*_pdf function that calls _weasyprint_render.
    Guards against a future refactor accidentally making the ReportLab
    fallback the primary path."""
    src = _strip_comments(_read("app/services/export.py"))
    # Find every `def _export_X_pdf_legacy(` — extract the X.
    legacy_names = re.findall(
        r"^def _export_(\w+)_pdf_legacy\(", src, re.MULTILINE)
    assert legacy_names, "no _legacy functions found — refactor error?"

    misses = []
    for name in legacy_names:
        # Find the public function that pairs with this legacy body.
        # Public path uses one of: export_{name}_pdf, or export_{name}
        # (for fmt-branched functions like export_cash_flow that route
        # PDF into their body).
        candidates = [f"export_{name}_pdf", f"export_{name}"]
        found_body = None
        for cand in candidates:
            m = re.search(
                r"^def " + re.escape(cand) + r"\([^)]*\):\n(.*?)(?=^def \w)",
                src, re.MULTILINE | re.DOTALL,
            )
            if m:
                found_body = m.group(1)
                break
        if found_body is None:
            misses.append(f"_export_{name}_pdf_legacy has no public counterpart")
            continue
        if "_weasyprint_render(" not in found_body:
            misses.append(
                f"_export_{name}_pdf_legacy pairs with public function "
                f"that no longer calls _weasyprint_render")
        if f"_export_{name}_pdf_legacy" not in found_body:
            misses.append(
                f"public paired function does not fall back to "
                f"_export_{name}_pdf_legacy")
    assert not misses, \
        "WeasyPrint-primary contract violations:\n  " + "\n  ".join(misses)
    return f"all {len(legacy_names)} legacy fallbacks correctly paired with WeasyPrint-first primaries"


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
