#!/usr/bin/env python3
"""MARSOUD-TKT-P3-EMAILS (Abdelhamid 2026-08-28) — customer-facing
emails must share one visual identity.

Design audit 2026-08-28 finding P0-06: `emails/invoice_sent.html` shipped
a NAVY gradient hero card while `emails/payslip.html` shipped a GREEN
table-header identity. Customers who received both (a tenant running
payroll + invoicing) read two different mailing identities. Exploration
also found `emails/invoice_reminder.html` used the same navy hero as
invoice_sent.

This ticket unified the two navy-hero emails (invoice_sent +
invoice_reminder) onto payslip's green table-header pattern. Choice
was ecosystem-driven, not palette-purist: both customer emails carry
a PDF attachment, and the invoice + payslip PDF templates already use
green table headers on navy text (see pdfs/invoice.html:52,
pdfs/payslip.html:31). Green-header email → green-header PDF now reads
as one system.

This audit is the regression net. Every check is a static file read +
substring assertion (no DB, no app bootstrap, <1s). If a future ticket
resurrects the navy gradient hero in either customer email, or removes
the green-header identity from any of the three, the check fails
loudly instead of shipping past a customer.

Explicitly NOT scoped:
- `task_notification.html` — bgcolor-locked by
  `audit_task_schedule_and_email.py:130-149` (Gmail-strips-gradients
  guardrail). Task assignment is not customer-facing.
- `subscription_reminder.html` — internal platform-billing domain,
  intentionally distinct.
- `emails/_base.html` shell header — the shell navy header stays;
  the audit finding was about the hero inside `{% block content %}`.

Checks:
  1. `invoice_sent.html` does not resurrect the navy gradient hero
     literal, and DOES declare the green table-header idiom
     (`background:#059669` on a `<th>`).
  2. Same for `invoice_reminder.html`. Its amber/red severity banner
     must remain (semantic, not brand).
  3. `payslip.html` still uses its green table-header block — the
     canonical reference must not silently drift.
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


def _read(relpath):
    return (ROOT / relpath).read_text(encoding="utf-8")


def _strip_comments(src):
    """Remove Jinja `{# … #}`, JS `//` line, and CSS `/* … */` comments so
    substring checks don't false-positive on retirement-doc notes in the
    source ("was navy hero, now green table"). Order matters — JS first
    so a JS comment containing `/*` can't open a spurious CSS-comment
    match. Same idiom used by `audit_brand_tokens_consumed.py` and
    `audit_a11y_baseline.py`."""
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


NAVY_HERO = "linear-gradient(135deg,#0A2540"
GREEN_HEADER = "background:#059669"


@check("1. invoice_sent.html: no navy hero, green table header present")
def _():
    src = _strip_comments(_read("app/templates/emails/invoice_sent.html"))
    assert NAVY_HERO not in src, (
        "invoice_sent.html still uses the retired navy gradient hero "
        f"({NAVY_HERO!r}) — unify with payslip.html's green table-header "
        "pattern."
    )
    assert GREEN_HEADER in src, (
        f"invoice_sent.html does not declare the unified {GREEN_HEADER!r} "
        "table-header identity — the customer sees a plain email that "
        "does not match the attached PDF's green headers."
    )
    return "navy hero retired, green table header present"


@check("2. invoice_reminder.html: no navy hero, severity banner preserved")
def _():
    src = _strip_comments(_read("app/templates/emails/invoice_reminder.html"))
    assert NAVY_HERO not in src, (
        "invoice_reminder.html still uses the retired navy gradient hero "
        f"({NAVY_HERO!r}) — same customer as invoice_sent, must share "
        "identity."
    )
    assert GREEN_HEADER in src, (
        f"invoice_reminder.html does not declare {GREEN_HEADER!r} — "
        "unify with payslip.html + invoice_sent.html."
    )
    # Semantic severity banner must survive the unification. This is
    # what tells the customer WHY they got this email (overdue vs
    # reminder); it is signaling, not brand, and must not be stripped.
    assert "accent_bg" in src and "accent_text" in src, (
        "invoice_reminder.html lost its amber/red severity banner "
        "signals — the accent_bg/accent_text Jinja variables must "
        "still drive the top banner. Overdue vs before-due is "
        "semantic; without it, the customer can't tell why they got "
        "the email at a glance."
    )
    return "navy hero retired, green table header present, severity banner preserved"


@check("3. payslip.html: canonical green identity still intact")
def _():
    src = _strip_comments(_read("app/templates/emails/payslip.html"))
    # payslip is the canonical reference this ticket unifies TOWARDS.
    # A future refactor that silently drops its green header would
    # re-open the drift.
    assert GREEN_HEADER in src, (
        "payslip.html no longer declares the canonical green table "
        "header — the reference identity for MARSOUD-TKT-P3-EMAILS is "
        "gone. If this was intentional, the other unified emails "
        "(invoice_sent, invoice_reminder) must be re-unified too."
    )
    # And payslip must also NOT have picked up a navy gradient hero
    # in some future refactor.
    assert NAVY_HERO not in src, (
        "payslip.html regressed to a navy gradient hero — the pattern "
        "the ticket explicitly retired. Fix or retire this audit."
    )
    return "canonical green header intact, no navy hero regression"


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
