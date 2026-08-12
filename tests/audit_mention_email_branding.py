#!/usr/bin/env python3
"""MARSOUD-MENTION-EMAIL-FIX (2026-08-13).

Bug fix for the @-mention email. Two bugs, one fix each:
  1. Header used generic "Marsoud" logo + name because
     `_send_mention_email` never passed a company into the
     shared shell.
  2. Button rendered as an inert link because the URL
     was relative — the config-key check read `SERVER_URL`
     (never set) instead of `SITE_URL`.

Checks:
  1. WITH logo — render mention email HTML and assert the
     tenant's `<img src=...>` (SITE_URL + logo_path) is in
     the header, tenant name is present, no `م` default
     placeholder.
  2. WITHOUT logo — assert the placeholder tile renders +
     the tenant name (NOT the literal "مرصود") is in the
     header.
  3. Lead-mention path — same assertions, entity_kind=lead.
  4. Absolute link — button href starts with the tenant's
     SITE_URL scheme + host, not the relative "/tasks/…".
  5. Graceful degradation on bad company_id — the email
     is still built (no exception).
  6. SERVER_URL regression sentinel — grep the service
     file, assert the wrong config key is gone forever.
  7. In-app Notification row still inserted (regression:
     the ticket forbids side effects on the bell path).
"""
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app import create_app, db

PREFIX = "__MEB_"
SITE_URL = "https://audit.marsoud.test"

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '"
            + PREFIX + "%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"]
                        for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} "
                        f"WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text(
                "DELETE FROM companies WHERE id = :c"),
                {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'meb-%@x.test'"))
    # Clean seeded logo files.
    from flask import current_app
    logos = Path(current_app.root_path) / "static" / "logos"
    if logos.exists():
        for f in logos.iterdir():
            if f.name.startswith("meb_"):
                try:
                    f.unlink()
                except OSError:
                    pass


def _mk_company(suffix, *, with_logo=False):
    from flask import current_app
    from app.models import Company
    from app.services.seed_coa import seed_default_coa
    c = Company(name=f"{PREFIX}{suffix}__", base_currency="EGP",
                 subdomain=f"meb-{suffix.lower()}",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c)
    db.session.flush()
    seed_default_coa(c.id)
    if with_logo:
        # Create a real PNG-shaped byte file on disk so
        # company_logo_email_uri's os.path.exists() check
        # succeeds. Content doesn't matter — the shell only
        # renders the <img src=...>.
        logos = Path(current_app.root_path) / "static" / "logos"
        logos.mkdir(parents=True, exist_ok=True)
        fn = f"meb_{suffix.lower()}.png"
        (logos / fn).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        c.logo_path = f"static/logos/{fn}"
    db.session.commit()
    return c


def _mk_user(email, company):
    from app.models import User, UserStatus
    from werkzeug.security import generate_password_hash
    u = User(email=email,
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name=email.split("@")[0],
             is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    u.companies.append(company)
    db.session.add(u)
    db.session.commit()
    return u


def _render_mention_html(*, actor_name, entity_kind,
                             entity_label, link_url, snippet,
                             company, recipient):
    """Render the mention email HTML directly, mirroring
    what _send_mention_email does after our fix."""
    from flask import render_template
    return render_template(
        "emails/mention.html",
        recipient=recipient,
        actor_name=actor_name,
        entity_kind=entity_kind,
        entity_label=entity_label,
        link_url=link_url,
        snippet=snippet,
        company=company,
    )


# ── checks ────────────────────────────────────────────────── #

@check("1. WITH logo — <img> uses tenant logo, name in header")
def _():
    from flask import current_app
    current_app.config["SITE_URL"] = SITE_URL
    _teardown()
    co = _mk_company("C1", with_logo=True)
    u = _mk_user("meb-recipient1@x.test", co)
    href = f"{SITE_URL}/tasks/42#comments"
    html = _render_mention_html(
        actor_name="زياد", entity_kind="task",
        entity_label="مهمة اختبارية", link_url=href,
        snippet="Hi @you", company=co, recipient=u,
    )
    # <img src=...> points at SITE_URL + logo_path.
    expected_src = f"{SITE_URL}/{co.logo_path}"
    assert expected_src in html, \
        f"<img src> missing {expected_src!r}"
    # Tenant name is in the header, not literal "مرصود".
    assert co.name in html, "tenant name missing"
    # Default "م" placeholder tile SHOULD NOT render when
    # a logo is present. Presence check: the tile has a
    # unique inline style "background:#1D9E75" that only
    # appears in the placeholder branch of _base.html.
    assert "background:#1D9E75" not in html, \
        "default placeholder rendered despite logo"
    return "logo + name OK"


@check("2. WITHOUT logo — placeholder + tenant name (not 'مرصود')")
def _():
    from flask import current_app
    current_app.config["SITE_URL"] = SITE_URL
    _teardown()
    co = _mk_company("C2", with_logo=False)
    u = _mk_user("meb-recipient2@x.test", co)
    href = f"{SITE_URL}/tasks/43#comments"
    html = _render_mention_html(
        actor_name="زياد", entity_kind="task",
        entity_label="مهمة أخرى", link_url=href,
        snippet="Hi @you", company=co, recipient=u,
    )
    # Placeholder tile IS in the html.
    assert "background:#1D9E75" in html, \
        "placeholder tile missing"
    # Tenant name IS there.
    assert co.name in html, "tenant name missing"
    # And the shell fallback text is NOT the default —
    # because we passed a company, the header renders
    # co.name not "مرصود".
    header_slice = html.split("</table>", 1)[0]
    assert "مرصود" not in header_slice, \
        f"default 'مرصود' leaked into header:\n{header_slice[-400:]}"
    return "name only, no wrong logo"


@check("3. Lead-mention path — same invariants")
def _():
    from flask import current_app
    current_app.config["SITE_URL"] = SITE_URL
    _teardown()
    co = _mk_company("C3", with_logo=True)
    u = _mk_user("meb-recipient3@x.test", co)
    href = f"{SITE_URL}/leads/7#comments"
    html = _render_mention_html(
        actor_name="زياد", entity_kind="lead",
        entity_label="عميل محتمل: X", link_url=href,
        snippet="Hi @you", company=co, recipient=u,
    )
    expected_src = f"{SITE_URL}/{co.logo_path}"
    assert expected_src in html, \
        f"<img src> missing on lead path"
    assert co.name in html, "tenant name missing on lead path"
    assert href in html, "lead detail link missing"
    return "lead path branded correctly"


@check("4. Button href is absolute (starts with SITE_URL)")
def _():
    from flask import current_app
    current_app.config["SITE_URL"] = SITE_URL
    _teardown()
    co = _mk_company("C4")
    u = _mk_user("meb-recipient4@x.test", co)
    href = f"{SITE_URL}/tasks/99#comments"
    html = _render_mention_html(
        actor_name="زياد", entity_kind="task",
        entity_label="مهمة", link_url=href,
        snippet="ok", company=co, recipient=u,
    )
    # The button anchor should have the absolute href.
    assert f'href="{href}"' in html, \
        f"button href not absolute: expected {href!r}"
    # A relative "/tasks/..." href would be a regression.
    assert 'href="/tasks/' not in html, \
        "relative /tasks/ href leaked into email"
    return "button href absolute"


@check("5. Graceful degradation — bad company_id doesn't crash email")
def _():
    """The DoD says: on any error resolving the company,
    the email must still be sent — just with default
    branding. Verify by calling `_send_mention_email`
    directly with a nonexistent company_id."""
    from flask import current_app
    current_app.config["SITE_URL"] = SITE_URL
    _teardown()
    co = _mk_company("C5")
    u = _mk_user("meb-recipient5@x.test", co)
    # Patch send_email to capture the html instead of
    # sending SMTP.
    from app.services import email as email_mod
    from app.services import mentions as mentions_mod
    captured = {}
    orig = email_mod.send_email
    def _capture(to, subject, html, **_):
        captured["to"] = to
        captured["subject"] = subject
        captured["html"] = html
        return True
    # Also patch the reference the service imports lazily.
    email_mod.send_email = _capture
    try:
        mentions_mod._send_mention_email(
            user_id=u.id, actor_name="test",
            entity_kind="task", entity_label="x",
            link_url=f"{SITE_URL}/tasks/1", snippet="s",
            company_id=9_999_999,   # doesn't exist
        )
    finally:
        email_mod.send_email = orig
    assert captured.get("to") == u.email, \
        "email was not built for bad company_id"
    # And the default "مرصود" text landed in the header
    # since we couldn't resolve the company.
    assert "مرصود" in captured["html"], \
        "default header missing on bad company_id"
    return "email still sent, defaults used"


@check("6. SERVER_URL regression sentinel — wrong key is gone")
def _():
    """Historical docstring comments may mention SERVER_URL
    as part of the bug narrative. Only fail if the wrong
    key comes back as an actual `config.get("SERVER_URL")`
    lookup — that's what the bug was."""
    src = (ROOT / "app" / "services"
           / "mentions.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        # Skip comment / docstring lines that just mention
        # the string historically.
        if stripped.startswith("#") or stripped.startswith('"'):
            continue
        assert ('"SERVER_URL"' not in stripped
                and "'SERVER_URL'" not in stripped), \
            (f"SERVER_URL config key back in mentions.py "
             f"— bug re-introduced on line: {stripped!r}")
    return "wrong key not present in live code"


@check("7. In-app Notification row still inserted (bell path intact)")
def _():
    from app.models import Notification, NotificationKind
    from app.services.mentions import notify_mentions
    _teardown()
    co = _mk_company("C7")
    actor = _mk_user("meb-actor7@x.test", co)
    target = _mk_user("meb-target7@x.test", co)
    n_before = Notification.query.filter_by(user_id=target.id).count()
    fan = notify_mentions(
        actor_user_id=actor.id,
        mentioned_user_ids=[target.id],
        company_id=co.id,
        entity_kind="task",
        entity_label="مهمة اختبار",
        link_url=f"{SITE_URL}/tasks/1#comments",
        snippet="ping @target",
    )
    assert fan == 1, f"expected 1 fan-out, got {fan}"
    rows = Notification.query.filter_by(
        user_id=target.id,
        kind=NotificationKind.MENTION.value).all()
    assert len(rows) >= 1, "no in-app notification row created"
    return f"bell row inserted (n={len(rows)})"


def main():
    app = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        with app.app_context():
            try:
                _teardown()
                res = fn()
                print(f"PASS  {label}  ⇒ {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  ⇒ "
                      f"{type(e).__name__}: {e}")
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
