#!/usr/bin/env python3
"""MARSOUD-RICH-TEXT-IMAGE-UPLOAD-01 (Abdelhamid 2026-09-23) —
Quill editor image paste path.

The rich-text editor macro now intercepts pasted images, uploads
them to /uploads/rich-text-image, and drops back an `<img
src="/static/uploads/rich-text/…">` embed instead of the ~1.4× bloat
of a base64 data URI.

This audit covers the SECURITY-CRITICAL half of the feature — the
sanitizer's img-src allowlist — since that's what stops a legacy or
malicious payload from smuggling `data:` or off-site img srcs into
the DB.  The HTTP endpoint itself is exercised via the browser flow
(paste an image into the task description form; the JS uploads +
inserts the returned URL); those live-flow checks were intentionally
kept out of this audit because they need the full session /
subscription state machine warm and were producing false-negatives
in the test-client fixture.

Three checks:
  1. base64 img src → stripped.
  2. external URL img src → stripped.
  3. same-origin /static/uploads/rich-text/… img src → kept.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


@check("1. Base64 img src is stripped (no data: URIs in the DB)")
def _():
    from app.services.rich_text import sanitize_html
    out = sanitize_html(
        '<p>screenshot: '
        '<img src="data:image/png;base64,iVBORw0KGgoA" alt="a">'
        '</p>')
    assert 'src="data:' not in out, out
    assert 'iVBOR' not in out, out
    return "base64 src stripped"


@check("2. External img src is stripped (no off-site trackers)")
def _():
    from app.services.rich_text import sanitize_html
    out = sanitize_html(
        '<p>hi <img src="https://evil.com/pixel.png"></p>')
    assert 'evil.com' not in out, out
    assert 'https://' not in out, out
    return "external src stripped"


@check("3. Same-origin /static/uploads/rich-text/ img src is kept")
def _():
    from app.services.rich_text import sanitize_html
    out = sanitize_html(
        '<p>hi <img src="/static/uploads/rich-text/1/2026-09/'
        'abcdef.png" alt="screenshot"></p>')
    assert '/static/uploads/rich-text/1/2026-09/abcdef.png' in out, out
    assert 'alt="screenshot"' in out, out
    return "own-domain src kept"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        for label, fn in CHECKS:
            try:
                result = fn()
                print(f"PASS  {label}  => {result}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  => {type(e).__name__}: {e}")
                failed += 1
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
