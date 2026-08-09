#!/usr/bin/env python3
"""MARSOUD-SUPERADMIN-CONTROL-01 T8 (2026-08-08) — route coverage
audit — locks the `flask audit-routes` CLI + admin viewer.

The ticket's permanent rule: 'مستحيل تتسلّم شاشة من غير طريقة
توصل ليها'. This suite is the automatic guard behind that rule.

Checks:
  1. build_coverage returns non-empty results shaped as expected
  2. Categories are the closed set {page, api, post_only,
     exempt, parametric_page}
  3. Zero orphans on the current tree (the CLI's success
     condition — locks the ignore file's correctness)
  4. Every ignore entry names an endpoint that ACTUALLY exists
     in url_map (no stale entries)
  5. Ignore file syntax rejects a reason-less line
  6. Detection works — a fake unlinked page is flagged as an
     orphan
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Checks ─────────────────────────────────────────────────────────

@check("1. build_coverage returns EndpointCoverage rows with expected shape")
def _(app):
    from app.services.route_audit import build_coverage, EndpointCoverage
    rows = build_coverage(app)
    assert rows, "build_coverage returned empty list"
    for r in rows[:5]:  # spot-check
        assert isinstance(r, EndpointCoverage)
        assert r.endpoint and r.url_rule
        assert isinstance(r.methods, frozenset)
        assert isinstance(r.linked_from, tuple)
    return f"{len(rows)} rows"


@check("2. Categories are the closed expected set")
def _(app):
    from app.services.route_audit import build_coverage
    ALLOWED = {"page", "parametric_page", "api", "post_only", "exempt"}
    rows = build_coverage(app)
    unknown = {r.category for r in rows} - ALLOWED
    assert not unknown, (
        f"unexpected category values: {unknown}")
    return f"categories = {sorted({r.category for r in rows})}"


@check("3. Zero orphans on the current tree")
def _(app):
    from app.services.route_audit import orphans
    orphs = orphans(app)
    assert not orphs, (
        f"{len(orphs)} orphan endpoint(s) — add a template link OR "
        f"add to route_audit_ignore.txt with a reason:\n  " +
        "\n  ".join(f"{r.endpoint}  ({r.url_rule})" for r in orphs[:10])
    )
    return "0 orphans"


@check("4. Every ignore entry names a real endpoint (no stale entries)")
def _(app):
    from app.services.route_audit import stale_ignores
    stale = stale_ignores(app)
    assert not stale, (
        f"{len(stale)} ignore entries name endpoints that no longer "
        f"exist (renamed / deleted):\n  " + "\n  ".join(stale)
    )
    return "no stale entries"


@check("5. Ignore file rejects a reason-less line with a clear error")
def _(app):
    """Point _IGNORE_FILE_NAME at a scratch file that has a bad
    line; assert build_coverage() raises with a helpful message."""
    from app.services import route_audit
    from app.services.route_audit import build_coverage
    # Write a temp ignore file with a valid line + a broken one
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        bad = td_path / route_audit._IGNORE_FILE_NAME
        bad.write_text(
            "# comment ok\n"
            "good.endpoint  # this is fine\n"
            "no_reason_here\n",
            encoding="utf-8"
        )
        # Monkeypatch the repo-root resolver so it looks in td
        orig = route_audit._repo_root
        route_audit._repo_root = lambda: td_path
        try:
            raised = False
            try:
                build_coverage(app)
            except ValueError as e:
                raised = "reason" in str(e).lower() or "غير" in str(e)
            assert raised, (
                "build_coverage should raise ValueError when the "
                "ignore file has a line without a `# reason` marker")
        finally:
            route_audit._repo_root = orig
    return "reason-less line raises with clear message"


@check("6. Detection works — a fake unlinked page is flagged as orphan")
def _(app):
    """Add a new route to the app that no template links to. Rebuild
    coverage. Assert it appears in orphans()."""
    from flask import Blueprint
    from app.services.route_audit import orphans
    bp = Blueprint("fake_orphan_bp", __name__)

    @bp.route("/__fake_orphan_endpoint__")
    def _v():
        return "orphan test"

    app.register_blueprint(bp)
    try:
        orphs = [r for r in orphans(app)
                 if r.endpoint == "fake_orphan_bp._v"]
        assert len(orphs) == 1, (
            f"the fake unlinked endpoint should have been flagged as "
            f"orphan; got {len(orphs)} matches")
    finally:
        # Blueprint registration is not easily reversible; the
        # audit stops running after this check so residual state
        # is fine. If we ever share this app across checks
        # ordered after 6, refactor to a fresh test app here.
        pass
    return "fake orphan detected"


def main():
    app = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        try:
            with app.app_context():
                result = fn(app)
            print(f"PASS  {label}\n        ⇒ {result}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
            failed += 1
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
