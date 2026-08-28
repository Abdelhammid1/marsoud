#!/usr/bin/env python3
"""MARSOUD-REJECTED-SIGNUPS-AUDIT (2026-08-27).

Full audit of محاولات التسجيل المرفوضة — the signup-rejection log,
the auto-learned domain/email blocklists, and the super-admin review
page at /admin/rejected-signups.

Two audits already cover parts of this: audit_signup_auto_block.py
proves the blocklist mechanics, audit_bot_registration.py proves the
TKT-17 visibility columns. This one covers what neither does — the
reason taxonomy as a whole, the guarantees the code claims in its own
docstrings, and the behaviours that are load-bearing but untested.

Checks:
  1. Schema — table, columns, and the reason CHECK constraint.
  2. Reason taxonomy — every gate writes the reason it claims to.
  3. Decoy indistinguishability — the three silent-reject paths return
     byte-identical responses, or a bot can fingerprint which gate it
     hit.
  4. record_rejection is best-effort — a broken log table must not
     raise into the signup form.
  5. Input normalisation — case, whitespace and malformed addresses.
  6. Unblock is idempotent and leaves an audit trail.
  7. Admin page renders rows and refuses non-superadmins.
  8. `bot_immediate` is unreachable — a documented dead value.
  9. maybe_auto_block's 3-in-24h counter is unreachable — dead branch.
 10. KNOWN RISK, asserted so a change surfaces: one bot honeypot trip
     permanently blocks a whole real domain, and later genuine signups
     from it fail silently with a success page.

Checks 8-10 assert what the code does TODAY, not what it should do.
They exist so that if someone changes it, the audit turns red and the
change is deliberate rather than accidental. See the report in the
ticket for why 10 is worth a product decision.
"""
import os
import sys
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import create_app, db  # noqa: E402

CHECKS = []

# Distinct from the other two audits' prefixes so a parallel run of
# either cannot delete this one's fixtures out from under it.
DOM = "rsaudit.test"
WHITE_DOM = "gmail.com"

ALL_REASONS = ("honeypot", "rate_limit", "spam_domain", "turnstile",
               "blocked_domain", "blocked_email", "bot_immediate")


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    """Remove everything this audit creates.

    The rate-limit scenario POSTs genuinely valid signups, so some of
    them complete and fan out into whatever tables the signup path
    touches — users, companies, user_companies, and an `employees` row
    for the owner. Deleting only the users and companies leaves those
    children behind as orphans, and SQLite then recycles the freed ids
    into the next audit's inserts: that is how a leftover `employees`
    row here surfaced as
    "UNIQUE constraint failed: employees.company_id, employees.user_id"
    — a 500 on a valid signup — inside audit_bot_protection. So sweep
    every company_id-bearing table for the companies made here, then
    the parents, then backstop on true orphans.
    """
    from sqlalchemy import text, inspect
    from app.services.bot_guard import register_rate_reset
    db.session.rollback()
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE subdomain LIKE 'rsa-%'"))]
        uids = [r[0] for r in conn.execute(text(
            "SELECT id FROM users WHERE email LIKE :e"),
            {"e": "%@" + DOM})]

        for cid in cids:
            for tbl in reversed(db.metadata.sorted_tables):
                try:
                    cols = {c["name"] for c in insp.get_columns(tbl.name)}
                except Exception:
                    continue
                if "company_id" in cols:
                    try:
                        conn.execute(text(
                            f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                            {"c": cid})
                    except Exception:
                        pass

        for uid in uids:
            for sql in ("DELETE FROM user_companies WHERE user_id = :u",
                        "DELETE FROM employees WHERE user_id = :u"):
                try:
                    conn.execute(text(sql), {"u": uid})
                except Exception:
                    pass

        for sql, params in (
                ("DELETE FROM signup_rejections WHERE email_domain IN "
                 "(:d, :w, '(unknown)', 'mailinator.com')",
                 {"d": DOM, "w": WHITE_DOM}),
                ("DELETE FROM blocked_domains WHERE domain IN (:d, :w)",
                 {"d": DOM, "w": WHITE_DOM}),
                ("DELETE FROM blocked_emails WHERE email LIKE :a "
                 "OR email LIKE :b",
                 {"a": "%@" + DOM, "b": "rsaudit-%@" + WHITE_DOM}),
                ("DELETE FROM platform_audit_logs WHERE details LIKE :d",
                 {"d": "%" + DOM + "%"}),
                ("DELETE FROM users WHERE email LIKE :e",
                 {"e": "%@" + DOM}),
                ("DELETE FROM companies WHERE subdomain LIKE 'rsa-%'", {}),
                # Backstops — anything this audit created and missed.
                ("DELETE FROM user_companies WHERE user_id NOT IN "
                 "(SELECT id FROM users)", {}),
                ("DELETE FROM employees WHERE company_id NOT IN "
                 "(SELECT id FROM companies)", {}),
        ):
            try:
                conn.execute(text(sql), params)
            except Exception:
                pass
    register_rate_reset()


def _post(client, email, *, honeypot=None, sub="rsa-x", ip=None):
    """One /register POST, in its OWN application context.

    `client` is accepted and ignored so call sites read naturally; the
    request has to run under a fresh app context regardless.

    Two things force this. `g` lives on the app context, and the
    request lifecycle caches `g.active_company` there, so reusing one
    context leaks identity between requests. Worse for this audit: the
    real signup path can 500 on an incompletely seeded dev DB, and a
    failed request leaves db.session in an aborted transaction — the
    NEXT request's record_rejection commit then fails, and because that
    write is deliberately best-effort the row simply never appears.
    The check fails with an empty list and nothing explains why.
    Production gives every request a fresh context; so does this.
    """
    from flask import current_app
    app = current_app._get_current_object()
    data = {"email": email, "full_name": "Audit", "company_name": "Audit Co",
            "subdomain": sub, "password": "Str0ngPass1!", "agree_terms": "on"}
    if honeypot is not None:
        data["website"] = honeypot
    headers = {"X-Forwarded-For": ip} if ip else {}
    with app.app_context():
        return app.test_client().post("/register", data=data,
                                      headers=headers)


def _reasons(domain=DOM):
    from app.models import SignupRejection
    # A POST that reaches the real signup path can 500 on an
    # incompletely seeded dev DB, leaving the session in a failed
    # transaction — the follow-up read then returns nothing and the
    # check fails for a reason that has nothing to do with the gate
    # under test. Roll back first so this reads committed state.
    db.session.rollback()
    return [r.reason for r in SignupRejection.query.filter_by(
        email_domain=domain).order_by(SignupRejection.id).all()]


# ── 1. schema ─────────────────────────────────────────────────────── #

@check("1. Schema — columns present and reason CHECK covers every value")
def _():
    import sqlalchemy as sa
    insp = sa.inspect(db.engine)
    tables = set(insp.get_table_names())
    for t in ("signup_rejections", "blocked_domains", "blocked_emails"):
        assert t in tables, f"missing table {t}"
    cols = {c["name"] for c in insp.get_columns("signup_rejections")}
    for c in ("email_domain", "email", "honeypot_value", "reason",
              "ip_address", "created_at"):
        assert c in cols, f"signup_rejections missing column {c}"
    # The constraint is the only thing stopping a typo'd reason from
    # becoming a garbage row, so assert it names all seven values.
    from app.models import SignupRejection
    ck = [c for c in SignupRejection.__table__.constraints
          if getattr(c, "sqltext", None) is not None]
    txt = " ".join(str(c.sqltext) for c in ck)
    missing = [r for r in ALL_REASONS if r not in txt]
    assert not missing, f"CHECK constraint omits {missing}"
    return f"{len(cols)} cols, CHECK covers all {len(ALL_REASONS)} reasons"


# ── 2. every gate writes the reason it claims ─────────────────────── #

@check("2. Reason taxonomy — each gate logs its own reason")
def _():
    from flask import current_app
    from app.services.signup_rejections import block_domain_now, block_email
    c = current_app.test_client()

    _post(c, f"hp@{DOM}", honeypot="http://spam.example")
    assert _reasons()[:1] == ["honeypot"], _reasons()
    _teardown()

    # blocked_domain: pre-block, then a clean POST must log it.
    block_domain_now(DOM, reason="audit")
    _post(c, f"any@{DOM}")
    assert _reasons() == ["blocked_domain"], _reasons()
    _teardown()

    # blocked_email: block one address on a whitelisted domain, so the
    # domain gate cannot be what fires.
    addr = f"rsaudit-one@{WHITE_DOM}"
    block_email(addr, reason="audit")
    _post(c, addr)
    assert _reasons(WHITE_DOM) == ["blocked_email"], _reasons(WHITE_DOM)
    _teardown()

    # rate_limit: six posts from one IP, last one trips.
    for i in range(6):
        _post(c, f"rl{i}@{DOM}", sub=f"rsa-rl{i}", ip="10.9.9.9")
    assert "rate_limit" in _reasons(), _reasons()
    _teardown()

    # spam_domain uses a disposable-mail domain, logged under its own
    # domain rather than DOM.
    from app.models import SignupRejection
    _post(c, "x@mailinator.com", sub="rsa-spam")
    got = SignupRejection.query.filter_by(
        email_domain="mailinator.com").first()
    assert got and got.reason == "spam_domain", got and got.reason
    SignupRejection.query.filter_by(
        email_domain="mailinator.com").delete(False)
    db.session.commit()
    return "honeypot / blocked_domain / blocked_email / rate_limit / spam_domain"


# ── 3. the decoy must not be fingerprintable ──────────────────────── #

@check("3. Decoy — silent-reject paths are byte-identical to a bot")
def _():
    from flask import current_app
    from app.services.signup_rejections import block_domain_now, block_email
    c = current_app.test_client()

    r_hp = _post(c, f"a@{DOM}", honeypot="spam")
    _teardown()
    block_domain_now(DOM, reason="audit")
    r_dom = _post(c, f"b@{DOM}")
    _teardown()
    addr = f"rsaudit-two@{WHITE_DOM}"
    block_email(addr, reason="audit")
    r_mail = _post(c, addr)

    codes = {r_hp.status_code, r_dom.status_code, r_mail.status_code}
    assert codes == {200}, f"status codes differ: {codes}"
    bodies = {r_hp.get_data(), r_dom.get_data(), r_mail.get_data()}
    assert len(bodies) == 1, (
        "decoy responses differ between gates — a bot can tell which "
        "one it hit and adapt")
    return "all three → identical 200"


# ── 4. the best-effort guarantee the docstring promises ───────────── #

@check("4. record_rejection never raises, even when the write fails")
def _():
    from app.services import signup_rejections as sr
    from app.models import SignupRejection

    real_add = db.session.add

    def boom(obj):
        if isinstance(obj, SignupRejection):
            raise RuntimeError("simulated log-table outage")
        return real_add(obj)

    db.session.add = boom
    try:
        # Must swallow. If this raises, a DB hiccup on the log table
        # becomes a 500 on the public signup form.
        sr.record_rejection("honeypot", f"x@{DOM}", ip_address="1.2.3.4")
    finally:
        db.session.add = real_add
        db.session.rollback()
    return "swallowed the outage"


# ── 5. normalisation + malformed input ────────────────────────────── #

@check("5. Blocklist lookups normalise case/whitespace, tolerate junk")
def _():
    from app.services.signup_rejections import (
        block_domain_now, block_email, is_domain_blocked, is_email_blocked,
        _extract_domain)

    assert _extract_domain("  A@Foo.COM ") == "foo.com"
    for junk in ("", None, "no-at-sign", "@", "trailing@"):
        assert is_domain_blocked(junk) is False, junk
        assert is_email_blocked(junk) is False, junk

    block_domain_now(DOM, reason="audit")
    for variant in (f"user@{DOM}", f"USER@{DOM.upper()}", f"  u@{DOM}  "):
        assert is_domain_blocked(variant) is True, variant

    addr = f"rsaudit-mixed@{WHITE_DOM}"
    block_email(addr, reason="audit")
    assert is_email_blocked(f"  RSAudit-Mixed@{WHITE_DOM.upper()}  ") is True
    return "case/space folded, junk safe"


# ── 6. unblock ────────────────────────────────────────────────────── #

@check("6. Unblock is idempotent and writes a platform audit entry")
def _():
    from app.models import BlockedDomain, PlatformAuditLog
    from app.services.signup_rejections import (
        block_domain_now, unblock_domain, is_domain_blocked)

    from flask import current_app
    block_domain_now(DOM, reason="audit")
    # log_platform_action stamps actor + IP off the request and
    # no-ops silently without one. Every real call site is inside a
    # request, so mirror that here.
    with current_app.test_request_context("/"):
        assert unblock_domain(DOM, actor_id=None) is True
        assert unblock_domain(DOM, actor_id=None) is False, "not idempotent"
    assert is_domain_blocked(f"u@{DOM}") is False

    row = BlockedDomain.query.filter_by(domain=DOM).first()
    assert row.is_active is False and row.unblocked_at is not None, \
        "unblock did not stamp the row"
    pal = PlatformAuditLog.query.filter(
        PlatformAuditLog.action == "signup_domain_unblocked",
        PlatformAuditLog.details.like(f"%{DOM}%")).first()
    assert pal is not None, "no audit-log entry for the unblock"
    return "lifted once, stamped, logged"


# ── 7. the admin page ─────────────────────────────────────────────── #

def _mk_user(email, *, superadmin):
    """A user complete enough to actually hold a session. Missing
    status / email_verified_at makes /admin/* answer 302 -> /login,
    which would silently turn the 403 assertion below into a pass for
    the wrong reason."""
    from datetime import datetime
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    u = User(email=email, full_name="rsa",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             is_superadmin=superadmin, is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u)
    db.session.commit()
    return u


@check("7. Admin page lists rows; non-superadmin gets 403")
def _():
    from flask import current_app
    from app.services.signup_rejections import block_domain_now

    block_domain_now(DOM, reason="audit marker")
    su = _mk_user(f"rsa-super@{DOM}", superadmin=True)
    plain = _mk_user(f"rsa-plain@{DOM}", superadmin=False)
    app = current_app._get_current_object()

    def as_user(uid):
        """One request, in its OWN app context.

        `g` lives on the application context, and the request
        lifecycle caches `g.active_company` / `g.user_companies` there
        (app/__init__.py). This audit holds a single long-lived app
        context open around every check, so two test clients used back
        to back inside it share that `g` — the second request keeps
        the FIRST user's identity and a non-superadmin sails through
        to 200. Production never sees this: every request there gets a
        fresh app context, which is exactly what this reproduces.
        """
        with app.app_context():
            c = app.test_client()
            with c.session_transaction() as sess:
                sess["_user_id"] = str(uid)
                sess["_fresh"] = True
            r = c.get("/admin/rejected-signups")
            return r.status_code, r.get_data(as_text=True)

    code, body = as_user(su.id)
    assert code == 200, f"superadmin got {code}"
    assert DOM in body, "the blocked domain is not rendered on the page"
    assert "لا يمكن قراءة سجل الرفض" not in body,         "page rendered its error banner on a healthy DB"

    code, _ = as_user(plain.id)
    assert code == 403, f"expected 403 for a non-superadmin, got {code}"
    return "renders for superadmin, 403 for everyone else"


# ── 8-10. current behaviour, asserted so a change is deliberate ───── #

@check("8. DEAD VALUE — `bot_immediate` is declared but never written")
def _():
    import subprocess
    out = subprocess.run(
        ["grep", "-rn", "--include=*.py", "bot_immediate", "app/"],
        capture_output=True, text=True, cwd=str(ROOT)).stdout
    writers = [ln for ln in out.splitlines()
               if "record_rejection(" in ln and "bot_immediate" in ln]
    assert not writers, (
        "bot_immediate now has a writer — update this check and the "
        "report: " + "; ".join(writers))
    return ("declared in the CHECK constraint, documented in the model "
            "and service docstrings, styled in the template — 0 writers")


@check("9. DEAD BRANCH — maybe_auto_block's 3-in-24h counter cannot fire")
def _():
    from flask import current_app
    from app.models import BlockedDomain
    c = current_app.test_client()
    # Five honeypot trips from one non-whitelisted domain. The first
    # blocks immediately (TKT-17), so trips 2-5 short-circuit at
    # is_domain_blocked and never reach the honeypot branch — the
    # counter can never reach its threshold of 3.
    for i in range(5):
        _post(c, f"c{i}@{DOM}", honeypot="spam", sub=f"rsa-c{i}")
    rs = _reasons()
    assert rs[0] == "honeypot", rs
    assert set(rs[1:]) == {"blocked_domain"}, rs
    row = BlockedDomain.query.filter_by(domain=DOM, is_active=True).first()
    assert row is not None
    assert not (row.reason or "").startswith("auto:"), (
        "the counter fired after all — maybe_auto_block is live again, "
        "update this check")
    return f"1 honeypot + {len(rs) - 1} blocked_domain; counter never used"


@check("10. KNOWN RISK — one bot trip silently locks out a real domain")
def _():
    from flask import current_app
    from app.models import User
    c = current_app.test_client()

    # A bot trips the honeypot once using a genuine company's domain.
    _post(c, f"bot@{DOM}", honeypot="http://spam.example", sub="rsa-bot")

    # A real employee at that company then signs up correctly.
    r = _post(c, f"ceo@{DOM}", sub="rsa-ceo")
    body = r.get_data(as_text=True)
    looks_ok = ("Registration received" in body) or ("Success" in body)
    created = User.query.filter_by(email=f"ceo@{DOM}").first() is not None

    assert r.status_code == 200 and looks_ok and not created, (
        "behaviour changed — the legitimate signup no longer fails "
        "silently. That is an improvement; update this check.")
    return ("legit signup → 200 success page, NO account, no alert "
            "(product decision pending)")


def main():
    app = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        with app.app_context():
            try:
                _teardown()
                res = fn()
                print(f"PASS  {label}\n        ⇒ {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback
                traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
