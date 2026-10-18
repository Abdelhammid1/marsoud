#!/usr/bin/env python3
"""MARSOUD-SMTP-PER-COMPANY-01 — per-tenant SMTP with safe fallback.

Seven checks + one integration validation.  Two fixture companies
(A + B) seeded with CoA so the cross-tenant test has somewhere to
try to peek at.  All SMTP sends are intercepted via monkey-patching
`smtplib.SMTP` / `SMTP_SSL` so the audit never contacts a real
mail server.

  1. Default path: company with no SMTP row → `send_email` targets
     the GLOBAL host (or log-only fallback when SMTP_HOST empty),
     source=default.
  2. Per-company path: company A gets a row → `send_email`
     intercepted call shows `host=mail.a.local, port=587`,
     source=company.
  3. No-silent-fallback: patched factory RAISES on connect for
     company A's host → `send_email` returns False, the default
     host is NEVER touched (counter stays 0), source=company logged.
  4. Delete falls back: delete A's row → next send goes default.
  5. Cross-tenant: user from company A tries to GET company B's
     SMTP settings → 403 or redirect.
  6. Password at rest: raw bytes in `password_encrypted` do NOT
     contain the plaintext; `crypto.decrypt` round-trips exactly.
  7. Password never logged: install log capture, exercise save + send
     + test paths, assert plaintext appears in zero records.

  INT. Integration: `send_invoice_cancelled_email` on a company with
       per-company SMTP → intercepted factory sees the company's host.
       Confirms ticket 1 picks up ticket 2 with zero code change on
       its side.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_A_NAME = "__SMTP_AUDIT_A__"
COMPANY_B_NAME = "__SMTP_AUDIT_B__"
_STATE = {}
_PLAINTEXT_PW = "ThePassword!987"


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


class _SMTPIntercept:
    """Records every SMTP connection attempt + login + send without
    talking to a real server.  Install via monkey-patching
    `smtplib.SMTP` and `smtplib.SMTP_SSL` with a factory that returns
    a `_FakeServer` wrapping this recorder.  Optionally raise on
    connect for a specific host (to simulate SMTP failure)."""
    def __init__(self):
        self.calls = []           # list of {"host", "port", "ssl"}
        self.logins = []          # list of {"user", "pw_len"}
        self.sends = []           # list of MIMEMessage objects
        self.raise_for_host = None

    def _factory(self, ssl=False):
        recorder = self
        class _FakeServer:
            def __init__(self, host, port, timeout=None):
                recorder.calls.append({"host": host, "port": port,
                                        "ssl": ssl})
                if (recorder.raise_for_host and
                        host == recorder.raise_for_host):
                    raise ConnectionRefusedError(
                        f"forced-fail {host}:{port}")
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): return (220, b"OK")
            def login(self, user, pw):
                recorder.logins.append({"user": user,
                                         "pw_len": len(pw)})
                return (235, b"OK")
            def send_message(self, msg):
                recorder.sends.append(msg); return {}
            def noop(self): return (250, b"OK")
        return _FakeServer


def _install_log_capture():
    cap_handler = logging.Handler()
    cap_handler.setLevel(logging.DEBUG)
    cap_handler.records = []
    cap_handler.emit = lambda r: cap_handler.records.append(r)
    for name in ("ledgeros.email", "ledgeros.invoicing",
                 "app.services.email", "app.services.crypto", ""):
        logging.getLogger(name).addHandler(cap_handler)
        logging.getLogger(name).setLevel(logging.DEBUG)
    return cap_handler


def _setup():
    from app.models import Company, Customer, PaymentMethod, Account
    from sqlalchemy import text

    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    for name in (COMPANY_A_NAME, COMPANY_B_NAME):
        for c in Company.query.filter_by(name=name).all():
            _teardown_company_hard(c.id)
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()

    from app.services.seed_coa import seed_default_coa
    from app.models import Plan
    plan = Plan.query.filter_by(is_active=True).first()

    def _mk(name):
        c = Company(name=name, base_currency="EGP")
        db.session.add(c); db.session.flush()
        seed_default_coa(c.id)
        cust = Customer(company_id=c.id, name=f"__cust_{name}__",
                         email=f"cust_{c.id}@audit.local")
        db.session.add(cust); db.session.flush()
        acc = Account.query.filter_by(
            company_id=c.id, code="1110").one()
        pm = PaymentMethod(company_id=c.id, name="cash",
                            name_ar="نقدي", account_id=acc.id,
                            is_active=True, is_default=True)
        db.session.add(pm); db.session.flush()
        if plan:
            c.intended_plan_id = plan.id
            c.plan_id = plan.id
        return c, cust, pm

    ca, custa, pma = _mk(COMPANY_A_NAME)
    cb, custb, pmb = _mk(COMPANY_B_NAME)
    _STATE["company_a"] = ca.id
    _STATE["company_b"] = cb.id
    _STATE["cust_a"] = custa.id
    _STATE["cust_b"] = custb.id
    _STATE["pm_a"] = pma.id
    _STATE["pm_b"] = pmb.id
    db.session.commit()


def _teardown_company_hard(company_id):
    from app.models import Company, JournalEntry, JournalLine
    from sqlalchemy import inspect, text
    db.session.rollback()
    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    insp = inspect(db.engine)
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(
            JournalLine.entry_id.in_(entry_ids)
        ).delete(synchronize_session=False)
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {col["name"] for col in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id)
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()


def _save_company_smtp(company_id, *, host, port=587,
                        encryption="STARTTLS",
                        username="smtp-user",
                        password=_PLAINTEXT_PW,
                        from_email="from@example.com",
                        from_name="Example Co",
                        is_enabled=True):
    """Persist a CompanySMTP row via the encryption helper (so the
    stored blob is the real on-disk format)."""
    from app.models import CompanySMTP
    from app.services.crypto import encrypt
    existing = CompanySMTP.query.filter_by(
        company_id=company_id).first()
    if existing:
        db.session.delete(existing); db.session.commit()
    row = CompanySMTP(
        company_id=company_id, host=host, port=port,
        encryption=encryption, username=username,
        password_encrypted=encrypt(password) if password else None,
        from_email=from_email, from_name=from_name,
        is_enabled=is_enabled)
    db.session.add(row); db.session.commit()
    return row


# ─── 1. Default path ──────────────────────────────────────────────
@check("1. no CompanySMTP row → source=default, no per-company host hit")
def _():
    from app.services.email import send_email
    import smtplib
    sentinel = _SMTPIntercept()
    orig_smtp, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP = sentinel._factory(ssl=False)
    smtplib.SMTP_SSL = sentinel._factory(ssl=True)
    try:
        # Dev config has no SMTP_HOST → send_email returns True via
        # log-only path.  Either outcome is acceptable as long as
        # NO per-company host was contacted (there is no row).
        send_email("to@example.com", "subj", "<p>body</p>",
                    company_id=_STATE["company_a"])
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_smtp, orig_ssl
    # If any SMTP connect happened at all, it must be to the global
    # config's host — never to a per-company host (none exists).
    for c in sentinel.calls:
        assert "a.local" not in (c["host"] or ""), \
            f"default path tried a per-company host: {c}"
    return f"default path OK (connects={len(sentinel.calls)})"


# ─── 2. Per-company path ──────────────────────────────────────────
@check("2. CompanySMTP row → send uses that host:port + from_email")
def _():
    from app.services.email import send_email
    import smtplib
    _save_company_smtp(_STATE["company_a"],
                        host="mail.a.local", port=2525,
                        from_email="from_a@a.local")
    sentinel = _SMTPIntercept()
    orig_smtp, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP = sentinel._factory(ssl=False)
    smtplib.SMTP_SSL = sentinel._factory(ssl=True)
    try:
        ok = send_email("to@example.com", "subj", "<p>body</p>",
                         company_id=_STATE["company_a"])
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_smtp, orig_ssl
    assert ok, "send_email reported failure on per-company path"
    assert len(sentinel.calls) == 1, \
        f"expected exactly 1 connect, got {sentinel.calls}"
    call = sentinel.calls[0]
    assert call["host"] == "mail.a.local", f"wrong host: {call}"
    assert call["port"] == 2525, f"wrong port: {call}"
    assert len(sentinel.sends) == 1, \
        f"expected 1 message, got {len(sentinel.sends)}"
    sent = sentinel.sends[0]
    from_header = sent.get("From") or ""
    assert "from_a@a.local" in from_header, \
        f"wrong From header: {from_header!r}"
    return (f"host={call['host']}:{call['port']}, "
            f"From={from_header!r}")


# ─── 3. No silent fallback on company-SMTP failure ────────────────
@check("3. per-company connect raises → return False, default NEVER tried")
def _():
    from app.services.email import send_email
    import smtplib
    _save_company_smtp(_STATE["company_a"],
                        host="mail.a.local", port=2525,
                        from_email="from_a@a.local")
    sentinel = _SMTPIntercept()
    sentinel.raise_for_host = "mail.a.local"
    orig_smtp, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP = sentinel._factory(ssl=False)
    smtplib.SMTP_SSL = sentinel._factory(ssl=True)
    try:
        ok = send_email("to@example.com", "subj", "<p>body</p>",
                         company_id=_STATE["company_a"])
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_smtp, orig_ssl
    assert ok is False, \
        f"company SMTP failed but send_email returned {ok}"
    # Exactly one attempt — the failing company host — must have
    # happened.  NO second attempt against the default platform host.
    hosts = [c["host"] for c in sentinel.calls]
    assert hosts == ["mail.a.local"], (
        f"silent fallback detected OR extra attempt: "
        f"hosts contacted = {hosts}")
    assert len(sentinel.sends) == 0, \
        f"a message was sent despite the connect refusal: {sentinel.sends}"
    return f"company SMTP failed cleanly, hosts contacted = {hosts}"


# ─── 4. Delete falls back ─────────────────────────────────────────
@check("4. delete the row → next send uses default (no per-company host)")
def _():
    from app.models import CompanySMTP
    from app.services.email import send_email
    import smtplib
    _save_company_smtp(_STATE["company_a"],
                        host="mail.a.local", port=2525,
                        from_email="from_a@a.local")
    # Now delete it, confirming the fallback.
    row = CompanySMTP.query.filter_by(
        company_id=_STATE["company_a"]).first()
    assert row is not None
    db.session.delete(row); db.session.commit()

    sentinel = _SMTPIntercept()
    orig_smtp, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP = sentinel._factory(ssl=False)
    smtplib.SMTP_SSL = sentinel._factory(ssl=True)
    try:
        send_email("to@example.com", "subj", "<p>body</p>",
                    company_id=_STATE["company_a"])
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_smtp, orig_ssl
    for c in sentinel.calls:
        assert "a.local" not in (c["host"] or ""), \
            f"deleted row still being used: {c}"
    return "fallback to default after delete OK"


# ─── 5. Cross-tenant isolation ────────────────────────────────────
@check("5. company A user cannot read/write company B's SMTP settings")
def _():
    from app.models import User
    from app.models.user import user_companies
    # Build a user owning ONLY company A.
    u = User.query.filter_by(email="__smtp_a_owner@audit.local").first()
    if not u:
        u = User(email="__smtp_a_owner@audit.local",
                  full_name="A owner", password_hash="x")
        db.session.add(u); db.session.flush()
    try:
        from app.services.legal import get_terms_version
        u.terms_version = get_terms_version() or ""
    except Exception:
        pass
    db.session.execute(user_companies.delete().where(
        user_companies.c.user_id == u.id))
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=_STATE["company_a"], role="owner"))
    db.session.commit()
    _STATE["user_a"] = u.id

    app = create_app()
    from flask import session as _flask_session
    from flask_login import login_user
    app.config["SESSION_PROTECTION"] = None
    with app.test_client() as client:
        with app.test_request_context():
            login_user(u)
            _flask_session["active_company_id"] = _STATE["company_a"]
            saved = dict(_flask_session)
        with client.session_transaction() as sess:
            sess.update(saved)
        # Cross-tenant GET.
        resp = client.get(
            f"/companies/{_STATE['company_b']}/smtp-settings",
            follow_redirects=False)
        assert resp.status_code in (302, 303, 403), (
            f"cross-tenant GET returned {resp.status_code} "
            f"(expected redirect/403)")
        # Cross-tenant POST (attempt to overwrite).
        resp = client.post(
            f"/companies/{_STATE['company_b']}/smtp-settings",
            data={"host": "evil.local", "port": "587",
                   "encryption": "STARTTLS",
                   "from_email": "evil@example.com"},
            follow_redirects=False)
        assert resp.status_code in (302, 303, 403), (
            f"cross-tenant POST returned {resp.status_code}")
    # Confirm company B still has no SMTP row (or the one it had
    # before — the POST must not have landed).
    from app.models import CompanySMTP
    row_b = CompanySMTP.query.filter_by(
        company_id=_STATE["company_b"]).first()
    if row_b is not None:
        assert row_b.host != "evil.local", \
            f"cross-tenant POST overwrote company B's config: {row_b.host}"
    return "cross-tenant GET + POST both refused"


# ─── 6. Password at rest is encrypted ─────────────────────────────
@check("6. password_encrypted bytes != plaintext; decrypt round-trips")
def _():
    from app.services.crypto import decrypt
    from sqlalchemy import text
    _save_company_smtp(_STATE["company_a"],
                        host="mail.a.local",
                        password="S3cret!Password_For_Audit")
    # Raw bytes via plain SQL — bypass the ORM so we see what's
    # actually on disk.
    row = db.session.execute(text(
        "SELECT password_encrypted FROM company_smtp "
        "WHERE company_id = :c"), {"c": _STATE["company_a"]}).first()
    assert row is not None
    raw: bytes = row[0]
    assert isinstance(raw, (bytes, bytearray, memoryview)), \
        f"expected bytes, got {type(raw)}"
    assert b"S3cret!Password_For_Audit" not in bytes(raw), \
        "plaintext leaked into stored blob — encryption is a no-op"
    assert decrypt(bytes(raw)) == "S3cret!Password_For_Audit", \
        "decrypt did not round-trip the stored password"
    return (f"ciphertext {len(raw)}B, plaintext NOT found, "
            f"round-trip OK")


# ─── 7. Password never appears in captured logs ───────────────────
@check("7. plaintext password never written to any log line")
def _():
    from app.services.email import send_email
    import smtplib
    cap = _install_log_capture()
    _save_company_smtp(_STATE["company_a"],
                        host="mail.a.local", port=2525,
                        password=_PLAINTEXT_PW,
                        from_email="from_a@a.local")
    sentinel = _SMTPIntercept()
    orig_smtp, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP = sentinel._factory(ssl=False)
    smtplib.SMTP_SSL = sentinel._factory(ssl=True)
    try:
        # Exercise both success and failure paths — the ticket's
        # "عدم تسجيل كلمات المرور في سجلات النظام" rule applies to
        # both.
        send_email("to@example.com", "subj", "<p>body</p>",
                    company_id=_STATE["company_a"])
        sentinel.raise_for_host = "mail.a.local"
        send_email("to@example.com", "subj2", "<p>body</p>",
                    company_id=_STATE["company_a"])
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_smtp, orig_ssl
    for r in cap.records:
        msg = r.getMessage()
        assert _PLAINTEXT_PW not in msg, (
            f"plaintext password leaked into log: {msg[:200]!r}")
        # Also check any stringified args.
        if r.args:
            for a in (r.args if isinstance(r.args, tuple) else [r.args]):
                assert _PLAINTEXT_PW not in str(a), (
                    f"plaintext password in log args: {str(a)[:200]!r}")
    return f"no plaintext in {len(cap.records)} captured log records"


# ─── 8. Live-send failure stamps last_send_* so UI can show it ────
# Gap-C fix: AC #8 says send failures must be surfaced to the
# operator, not just logged.  New `last_send_*` columns on
# CompanySMTP get updated by `send_email` on company-source
# attempts; this check pins that round-trip.
@check("8. live-send failure stamps last_send_at + last_send_error on the row")
def _():
    from app.models import CompanySMTP
    from app.services.email import send_email
    import smtplib
    _save_company_smtp(_STATE["company_a"],
                        host="mail.a.local", port=2525,
                        from_email="from_a@a.local")
    # Make sure stamps start clean.
    row = CompanySMTP.query.filter_by(
        company_id=_STATE["company_a"]).first()
    row.last_send_at = None
    row.last_send_ok = None
    row.last_send_error = None
    db.session.commit()

    sentinel = _SMTPIntercept()
    sentinel.raise_for_host = "mail.a.local"
    orig_smtp, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP = sentinel._factory(ssl=False)
    smtplib.SMTP_SSL = sentinel._factory(ssl=True)
    try:
        ok = send_email("to@example.com", "subj", "<p>body</p>",
                         company_id=_STATE["company_a"])
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_smtp, orig_ssl
    assert ok is False
    db.session.expire_all()
    row2 = CompanySMTP.query.filter_by(
        company_id=_STATE["company_a"]).first()
    assert row2.last_send_at is not None, \
        "last_send_at did NOT get stamped on failure"
    assert row2.last_send_ok is False, \
        f"expected last_send_ok=False, got {row2.last_send_ok}"
    assert row2.last_send_error and "ConnectionRefused" in row2.last_send_error, \
        f"error text missing or wrong: {row2.last_send_error!r}"

    # Now a success → stamp flips back to ok=True, error cleared.
    sentinel.raise_for_host = None
    smtplib.SMTP = sentinel._factory(ssl=False)
    smtplib.SMTP_SSL = sentinel._factory(ssl=True)
    try:
        ok = send_email("to@example.com", "subj", "<p>body</p>",
                         company_id=_STATE["company_a"])
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_smtp, orig_ssl
    assert ok is True
    db.session.expire_all()
    row3 = CompanySMTP.query.filter_by(
        company_id=_STATE["company_a"]).first()
    assert row3.last_send_ok is True, \
        f"expected last_send_ok=True after success, got {row3.last_send_ok}"
    assert row3.last_send_error is None, \
        f"error not cleared after success: {row3.last_send_error!r}"
    return (f"fail stamped error, success cleared it — "
            f"last_send_at={row3.last_send_at}")


# ─── 9. Test endpoint actually SENDS a message (Gap A) ────────────
@check("9. test endpoint sends a real test email, not just NOOP")
def _():
    from app.models import User
    from app.models.user import user_companies
    import smtplib
    _save_company_smtp(_STATE["company_a"],
                        host="mail.a.local", port=2525,
                        from_email="from_a@a.local")

    # Reuse the owner from check 5 (has company_a access).
    u = User.query.filter_by(
        email="__smtp_a_owner@audit.local").first()
    assert u is not None, "check 5 should have created this user"

    sentinel = _SMTPIntercept()
    orig_smtp, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP = sentinel._factory(ssl=False)
    smtplib.SMTP_SSL = sentinel._factory(ssl=True)

    app = create_app()
    from flask import session as _flask_session
    from flask_login import login_user
    app.config["SESSION_PROTECTION"] = None
    try:
        with app.test_client() as client:
            with app.test_request_context():
                login_user(u)
                _flask_session["active_company_id"] = _STATE["company_a"]
                saved = dict(_flask_session)
            with client.session_transaction() as sess:
                sess.update(saved)
            resp = client.post(
                f"/companies/{_STATE['company_a']}/smtp-settings/test",
                follow_redirects=False)
            assert resp.status_code in (302, 303)
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_smtp, orig_ssl

    # The test endpoint MUST have sent an actual message, not just
    # a NOOP — the ticket says "اختبار الاتصال وإرسال بريد تجريبي".
    assert len(sentinel.sends) == 1, (
        f"test endpoint didn't send a real message — "
        f"sends={len(sentinel.sends)}")
    sent = sentinel.sends[0]
    subject = sent.get("Subject") or ""
    assert "اختبار SMTP" in subject, \
        f"test email subject wrong: {subject!r}"
    return f"test endpoint sent 1 message with subject={subject!r}"


# ─── 10. Pre-save test uses form values (Gap B) ───────────────────
@check("10. pre-save test uses form values, does not stamp saved row")
def _():
    from app.models import User, CompanySMTP
    from app.models.user import user_companies
    import smtplib

    # Save a config → will later confirm its last_test_* stays
    # untouched when we POST form-only test data.
    _save_company_smtp(_STATE["company_a"],
                        host="mail.saved.local", port=2525,
                        from_email="saved@a.local")
    row_before = CompanySMTP.query.filter_by(
        company_id=_STATE["company_a"]).first()
    pre_test_at = row_before.last_test_at

    u = User.query.filter_by(
        email="__smtp_a_owner@audit.local").first()

    sentinel = _SMTPIntercept()
    orig_smtp, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP = sentinel._factory(ssl=False)
    smtplib.SMTP_SSL = sentinel._factory(ssl=True)

    app = create_app()
    from flask import session as _flask_session
    from flask_login import login_user
    app.config["SESSION_PROTECTION"] = None
    try:
        with app.test_client() as client:
            with app.test_request_context():
                login_user(u)
                _flask_session["active_company_id"] = _STATE["company_a"]
                saved = dict(_flask_session)
            with client.session_transaction() as sess:
                sess.update(saved)
            # POST form-only values — different host than the stored
            # row.  Pre-save test should hit the form host, not the
            # saved one, and MUST NOT stamp the saved row.
            resp = client.post(
                f"/companies/{_STATE['company_a']}/smtp-settings/test",
                data={
                    "host": "mail.pretest.local", "port": "2525",
                    "encryption": "STARTTLS",
                    "from_email": "pretest@a.local",
                    "password": "typed-in-form",
                },
                follow_redirects=False)
            assert resp.status_code in (302, 303)
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_smtp, orig_ssl

    hosts = [c["host"] for c in sentinel.calls]
    assert hosts == ["mail.pretest.local"], (
        f"pre-save test did NOT use form host: hosts={hosts}")
    db.session.expire_all()
    row_after = CompanySMTP.query.filter_by(
        company_id=_STATE["company_a"]).first()
    assert row_after.last_test_at == pre_test_at, (
        f"pre-save test incorrectly stamped the saved row: "
        f"was {pre_test_at}, now {row_after.last_test_at}")
    return (f"pre-save test used form host {hosts!r}, "
            f"saved row untouched")


# ─── INT. Integration: ticket 1 picks up per-company SMTP ─────────
@check("INT. send_invoice_cancelled_email uses company SMTP automatically")
def _():
    from app.models import Invoice, InvoiceItem
    from app.models.invoice import InvoiceStatus
    from app.services.email import send_invoice_cancelled_email
    from app.services.numbering import next_number
    from app.services.invoicing import post_invoice_to_ledger
    import smtplib

    _save_company_smtp(_STATE["company_a"],
                        host="mail.integration.local", port=2525,
                        from_email="int@a.local")

    inv = Invoice(
        company_id=_STATE["company_a"],
        customer_id=_STATE["cust_a"],
        number=next_number(_STATE["company_a"], "INVOICE"),
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="EGP", tax_rate=0,
        status=InvoiceStatus.DRAFT,
    )
    db.session.add(inv); db.session.flush()
    item = InvoiceItem(
        invoice_id=inv.id, company_id=_STATE["company_a"],
        description="x", quantity=1, unit_price=500.0)
    db.session.add(item); db.session.flush()
    inv.recalc()
    db.session.flush()
    inv.status = InvoiceStatus.SENT
    post_invoice_to_ledger(inv)
    # Pretend it was voided so the body has reason + voided_at.
    from datetime import datetime as _dt
    inv.void_reason = "integration test cancel"
    inv.voided_at = _dt.utcnow()
    db.session.commit()

    sentinel = _SMTPIntercept()
    orig_smtp, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP = sentinel._factory(ssl=False)
    smtplib.SMTP_SSL = sentinel._factory(ssl=True)
    try:
        send_invoice_cancelled_email(inv)
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_smtp, orig_ssl
    hosts = [c["host"] for c in sentinel.calls]
    assert "mail.integration.local" in hosts, (
        f"cancel email did NOT use the per-company SMTP: "
        f"hosts contacted = {hosts}")
    return f"ticket 1's cancel email went through company SMTP: {hosts}"


# ─── INT2. Gap-D fix: cancel-email on no-SMTP company uses default
@check("INT2. cancel-email on company WITHOUT per-company SMTP uses default")
def _():
    from app.models import Invoice, InvoiceItem, CompanySMTP
    from app.models.invoice import InvoiceStatus
    from app.services.email import send_invoice_cancelled_email
    from app.services.numbering import next_number
    from app.services.invoicing import post_invoice_to_ledger
    import smtplib

    # Company B has no SMTP row.  Confirm.
    assert CompanySMTP.query.filter_by(
        company_id=_STATE["company_b"]).first() is None, (
        "company_b should have no SMTP row for this check")

    inv = Invoice(
        company_id=_STATE["company_b"],
        customer_id=_STATE["cust_b"],
        number=next_number(_STATE["company_b"], "INVOICE"),
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="EGP", tax_rate=0,
        status=InvoiceStatus.DRAFT,
    )
    db.session.add(inv); db.session.flush()
    item = InvoiceItem(
        invoice_id=inv.id, company_id=_STATE["company_b"],
        description="x", quantity=1, unit_price=250.0)
    db.session.add(item); db.session.flush()
    inv.recalc()
    db.session.flush()
    inv.status = InvoiceStatus.SENT
    post_invoice_to_ledger(inv)
    from datetime import datetime as _dt
    inv.void_reason = "default-path integration test"
    inv.voided_at = _dt.utcnow()
    db.session.commit()

    sentinel = _SMTPIntercept()
    orig_smtp, orig_ssl = smtplib.SMTP, smtplib.SMTP_SSL
    smtplib.SMTP = sentinel._factory(ssl=False)
    smtplib.SMTP_SSL = sentinel._factory(ssl=True)
    try:
        send_invoice_cancelled_email(inv)
    finally:
        smtplib.SMTP, smtplib.SMTP_SSL = orig_smtp, orig_ssl
    # Must NOT touch any per-company host (company_b has none).  If
    # dev has SMTP_HOST unset, this goes log-only and no connect
    # happens — also fine.  The important assertion: no connect to
    # any `*.local` test-fixture host that would imply the resolver
    # was confused.
    for c in sentinel.calls:
        assert "integration.local" not in (c["host"] or ""), (
            f"default-path cancel email leaked to company A's "
            f"SMTP host: {c}")
    return (f"cancel email on no-SMTP company B used default path "
            f"({len(sentinel.calls)} connects, none to A's host)")


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:
                    print(f"FAIL  {label}\n        => "
                          f"{type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                for key in ("company_a", "company_b"):
                    if key in _STATE:
                        _teardown_company_hard(_STATE[key])
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
