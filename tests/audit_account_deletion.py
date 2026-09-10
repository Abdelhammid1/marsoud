#!/usr/bin/env python3
"""MARSOUD-ACCOUNT-DELETION-01 (2026-09-11) — /account/delete flow.

Apple App Store guideline 5.1.1(v) and Google Play both require an
in-app account deletion path.  This suite guards the semantics.

Checks:
  1. Preview — user is a member only (not owner) → membership
     removed, company stays.
  2. Preview — user is sole owner + no other members → company
     scheduled for full delete.
  3. Preview — user is sole owner + has other members → refused
     with a blocker (Apple accepts this as long as the user CAN
     eventually delete after resolving it).
  4. Execution — happy path: sole owner + no others.  User row +
     company + user_companies rows all gone after.
  5. Execution — membership-only path.  User row gone, company
     stays, user_companies row for (user, company) is removed.
  6. Execution — blocker: raises AccountDeletionError, nothing
     changes.
  7. Route surface — GET /account/delete renders 200 for a logged
     in user.
  8. Route surface — POST without the confirm_email field bounces
     back to the form (does NOT delete).
  9. Landing page footer links `/privacy` + `/terms` + /account/
     delete instead of `#`.
 10. base.html user-menu has the "سياسة الخصوصية" +
     "حذف حسابي" entries above the logout row.
"""
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from decimal import Decimal

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _boot(prefix):
    """Fresh company + owner. Returns (company_id, owner_id, owner_email).
    Cleans up existing rows with the same prefix."""
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE :p"),
        {"p": f"__{prefix}__%"})]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"),
                    {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE :p"),
        {"p": f"%__{prefix.lower()}__%"})
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C",
                    allowed_subitems=None)
        db.session.add(plan)
    plan.set_modules(["accounting"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"
    email = f"owner__{prefix.lower()}__@x.io"
    owner = User(email=email,
                 full_name=f"Owner {prefix}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv, terms_accepted_at=datetime.utcnow())
    owner.set_password("pw12345678")
    db.session.add(owner); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()
    return c.id, owner.id, email


def _add_member(company_id, email_suffix, role="accountant"):
    """Attach a second user to a company. Returns the new user's id."""
    from app import db
    from app.models import User
    from app.models.user import user_companies
    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"
    u = User(email=f"m__{email_suffix}__@x.io",
             full_name=f"Member {email_suffix}", is_active=True,
             email_verified_at=datetime.utcnow(),
             terms_version=tv, terms_accepted_at=datetime.utcnow())
    u.set_password("pw12345678")
    db.session.add(u); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=company_id, role=role))
    db.session.commit()
    return u.id


@check("1. Preview — member (not owner) → membership removed, "
        "company stays")
def _():
    from app import create_app, db
    from app.models import User
    from app.services.account_deletion import preview_deletion
    app = create_app()
    with app.app_context():
        cid, oid, _oemail = _boot("DEL1")
        mid = _add_member(cid, "del1a", role="accountant")
        member = db.session.get(User, mid)
        plan = preview_deletion(member)
        assert len(plan["memberships_to_remove"]) == 1
        assert plan["memberships_to_remove"][0].id == cid
        assert not plan["companies_to_delete"]
        assert not plan["blockers"]
        assert plan["can_proceed"] is True
        return "membership-only: 1 removal, 0 deletions, no blockers"


@check("2. Preview — sole owner + no other members → company "
        "queued for deletion")
def _():
    from app import create_app, db
    from app.models import User
    from app.services.account_deletion import preview_deletion
    app = create_app()
    with app.app_context():
        cid, oid, _ = _boot("DEL2")
        owner = db.session.get(User, oid)
        plan = preview_deletion(owner)
        assert len(plan["companies_to_delete"]) == 1
        assert plan["companies_to_delete"][0].id == cid
        assert not plan["memberships_to_remove"]
        assert not plan["blockers"]
        assert plan["can_proceed"] is True
        return "sole-owner-alone: 1 deletion, 0 removals, no blockers"


@check("3. Preview — sole owner + other members → blocker")
def _():
    from app import create_app, db
    from app.models import User
    from app.services.account_deletion import preview_deletion
    app = create_app()
    with app.app_context():
        cid, oid, _ = _boot("DEL3")
        _add_member(cid, "del3a", role="accountant")
        owner = db.session.get(User, oid)
        plan = preview_deletion(owner)
        assert not plan["companies_to_delete"]
        assert not plan["memberships_to_remove"]
        assert len(plan["blockers"]) == 1
        assert plan["blockers"][0][0].id == cid
        assert plan["can_proceed"] is False
        return "sole-owner-plus-others: 1 blocker, refuses"


@check("4. Execution — sole owner + no others: user + company + "
        "membership rows all gone after delete")
def _():
    from app import create_app, db
    from app.models import User, Company
    from app.models.user import user_companies
    from sqlalchemy import text
    from app.services.account_deletion import delete_account
    app = create_app()
    with app.app_context():
        cid, oid, _ = _boot("DEL4")
        owner = db.session.get(User, oid)
        res = delete_account(owner)
        assert res["companies_deleted"] == 1
        assert res["memberships_removed"] == 0
        assert db.session.get(User, oid) is None
        assert db.session.get(Company, cid) is None
        # And no dangling user_companies row.
        rows = db.session.execute(text(
            "SELECT * FROM user_companies "
            "WHERE user_id = :u OR company_id = :c"),
            {"u": oid, "c": cid}).fetchall()
        assert not rows, f"dangling user_companies rows: {rows}"
        return "user + company + memberships all cleaned"


@check("5. Execution — membership-only: user gone, company stays")
def _():
    from app import create_app, db
    from app.models import User, Company
    from app.models.user import user_companies
    from sqlalchemy import text
    from app.services.account_deletion import delete_account
    app = create_app()
    with app.app_context():
        cid, _oid, _ = _boot("DEL5")
        mid = _add_member(cid, "del5a", role="accountant")
        member = db.session.get(User, mid)
        res = delete_account(member)
        assert res["companies_deleted"] == 0
        assert res["memberships_removed"] == 1
        assert db.session.get(User, mid) is None
        assert db.session.get(Company, cid) is not None
        rows = db.session.execute(text(
            "SELECT * FROM user_companies WHERE user_id = :u"),
            {"u": mid}).fetchall()
        assert not rows
        return "user gone, company survives"


@check("6. Execution — blocker: raises AccountDeletionError, "
        "user + company both untouched")
def _():
    from app import create_app, db
    from app.models import User, Company
    from app.services.account_deletion import (
        delete_account, AccountDeletionError,
    )
    app = create_app()
    with app.app_context():
        cid, oid, _ = _boot("DEL6")
        _add_member(cid, "del6a", role="accountant")
        owner = db.session.get(User, oid)
        try:
            delete_account(owner)
        except AccountDeletionError as e:
            assert e.code == "blocked"
            assert e.blockers
        else:
            raise AssertionError("expected AccountDeletionError")
        # Nothing changed.
        assert db.session.get(User, oid) is not None
        assert db.session.get(Company, cid) is not None
        return "refused; both rows still there"


@check("7. GET /account/delete renders 200 for a logged-in user")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        cid, oid, _ = _boot("DEL7")
        client = app.test_client()
        with client.session_transaction() as s:
            s["_user_id"] = str(oid)
            s["active_company_id"] = cid
        r = client.get("/account/delete")
        assert r.status_code == 200, r.status_code
        body = r.data.decode("utf-8")
        assert "حذف حسابي" in body
        return "page renders + Arabic title present"


@check("8. POST /account/delete without confirm_email → does NOT "
        "delete, redirects back to the form")
def _():
    from app import create_app, db
    from app.models import User
    app = create_app()
    with app.app_context():
        cid, oid, _ = _boot("DEL8")
        client = app.test_client()
        with client.session_transaction() as s:
            s["_user_id"] = str(oid)
            s["active_company_id"] = cid
        r = client.post("/account/delete", data={})
        # Route redirects back to /account/delete on bad input.
        assert r.status_code in (302, 303), r.status_code
        # And the user is still there.
        assert db.session.get(User, oid) is not None
        return "empty POST bounces, user untouched"


@check("9. Landing footer wires `/privacy`, `/terms`, "
        "/account/delete — no `href=\"#\"` on legal links")
def _():
    p = ROOT / "app" / "templates" / "landing.html"
    txt = p.read_text(encoding="utf-8")
    # Grab just the legal foot-col block, then strip HTML + Jinja
    # comments so a documentation snippet containing `href="#"` (or
    # the ticket ID) doesn't false-flag the check.
    import re
    m = re.search(
        r'<h4>قانوني</h4>(.*?)</div>', txt, flags=re.DOTALL)
    assert m, "couldn't locate the قانوني footer block"
    block = m.group(1)
    block_clean = re.sub(r'<!--.*?-->', '', block, flags=re.DOTALL)
    block_clean = re.sub(r'\{#.*?#\}', '', block_clean, flags=re.DOTALL)
    assert 'href="#"' not in block_clean, (
        "landing footer legal block still has a broken `#` link "
        f"(cleaned block: {block_clean[:200]!r})")
    assert '/privacy' in block_clean, "no /privacy link in footer"
    assert '/terms' in block_clean, "no /terms link in footer"
    assert ("delete_account" in block_clean
            or "account/delete" in block_clean), (
        "no delete-account link in landing footer")
    return "footer wires /privacy + /terms + /account/delete"


@check("10. base.html user menu carries the privacy + delete-account "
        "entries above the logout button")
def _():
    p = ROOT / "app" / "templates" / "base.html"
    txt = p.read_text(encoding="utf-8")
    # Presence of both markers. base.html has MULTIPLE logout forms
    # (sidebar footer + top-bar user menu); assert the entries land
    # immediately before the LAST one (the user-menu form), not the
    # first one (the sidebar form which comes before the menu).
    priv_idx = txt.find("public.privacy")
    del_idx = txt.find("auth.delete_account")
    last_logout_idx = txt.rfind("auth.logout")
    assert priv_idx != -1, "privacy link missing from base.html"
    assert del_idx != -1, "delete-account link missing from base.html"
    assert priv_idx < last_logout_idx, (
        "privacy entry must appear before the last logout form "
        f"(priv @ {priv_idx}, last logout @ {last_logout_idx})")
    assert del_idx < last_logout_idx, (
        "delete-account entry must appear before the last logout form")
    # And they should be close together — inside the user menu dropdown.
    # Guard against a future edit that puts them in some other card.
    assert last_logout_idx - priv_idx < 4000, (
        "privacy link is too far above the last logout form — is it "
        "still inside the user menu dropdown?")
    return "both entries present + ordered above the user-menu logout"


def main():
    from app import create_app
    _ = create_app()
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
