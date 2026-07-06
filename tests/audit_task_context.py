#!/usr/bin/env python3
"""MARSOUD-TASK-CONTEXT — unit-level audit for the return_to plumbing
added on 2026-07-06.

The E2E in /tmp/task-context-e2e.py already proves the happy path
(create/edit/delete from the employee drill-down land back on the
drill-down). This file exercises the security-critical guard:
_safe_next must refuse ANY non-local URL so the return_to parameter
can't be flipped into an open-redirect / phishing hop.
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


@check("empty string → default")
def _():
    from app.routes.tasks import _safe_next
    with app.test_request_context("/", method="POST", data={"return_to": ""}):
        assert _safe_next("/default") == "/default"
    return "OK"


@check("local path with querystring → allowed")
def _():
    from app.routes.tasks import _safe_next
    val = "/tasks/?scope=employees&user_id=15"
    with app.test_request_context("/", method="POST", data={"return_to": val}):
        assert _safe_next("/default") == val
    return f"kept {val!r}"


@check("external absolute URL → default")
def _():
    from app.routes.tasks import _safe_next
    for val in ("https://attacker.com/steal",
                "http://evil.example/x",
                "javascript:alert(1)"):
        with app.test_request_context("/", method="POST", data={"return_to": val}):
            assert _safe_next("/default") == "/default", \
                f"open-redirect not blocked for {val!r}"
    return "all 3 rejected"


@check("protocol-relative //x → default")
def _():
    from app.routes.tasks import _safe_next
    with app.test_request_context("/", method="POST",
                                    data={"return_to": "//attacker.com/x"}):
        assert _safe_next("/default") == "/default"
    return "'//' rejected"


@check("CRLF header injection → default")
def _():
    from app.routes.tasks import _safe_next
    for val in ("/legit\r\nSet-Cookie: pwned=1",
                "/legit\nLocation: /bad"):
        with app.test_request_context("/", method="POST", data={"return_to": val}):
            assert _safe_next("/default") == "/default", \
                f"CRLF not blocked: {val!r}"
    return "both rejected"


@check("falls back to query-string when form is empty")
def _():
    from app.routes.tasks import _safe_next
    with app.test_request_context("/?return_to=%2Ftasks%2F%3Fscope%3Dall",
                                    method="GET"):
        # decoded value should be the local path
        assert _safe_next("/default") == "/tasks/?scope=all"
    return "GET query-string honoured"


@check("form value wins over query-string")
def _():
    from app.routes.tasks import _safe_next
    with app.test_request_context("/?return_to=/from-query",
                                    method="POST",
                                    data={"return_to": "/from-form"}):
        assert _safe_next("/default") == "/from-form"
    return "POST field takes precedence"


def main():
    global app
    app = create_app()
    passed = failed = 0
    with app.app_context():
        for label, fn in CHECKS:
            try:
                result = fn()
                print(f"PASS  {label}  ⇒ {result}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback
                traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
