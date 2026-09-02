#!/usr/bin/env python3
"""MARSOUD-COMPANY-BRANCHES-01 (2026-09-02) — company branches +
consolidated reports.

Everything infrastructure-side already existed (parent_id,
count_branches, QUOTA_BRANCHES). This ticket wires the UI + adds
two read-side consolidated report functions.

Checks:
  1. /companies/<id>/branches route registered.
  2. Two consolidated report routes registered.
  3. Link a branch to a parent → parent_id set + shows in list.
  4. Cross-tenant link refused (branch not owned by the user).
  5. Multi-level hierarchy refused (branch already has parent_id).
  6. `consolidated_income_statement(parent, ...)` sums equal to Σ
     per-branch numbers.
  7. Unlink route (branch mode POST action=unlink) → parent_id=None
     and the branch's own data is intact.
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


def _boot_two_owned_companies(prefix, *, count=2):
    """Create ONE owner + `count` companies they own, all siblings
    (no parent). Returns (owner_email, owner_id, [company_ids])."""
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
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C")
        db.session.add(plan)
    plan.set_modules(["accounting", "sales", "reports"])
    db.session.flush()

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"
    owner = User(email=f"owner__{prefix.lower()}__@x.io",
                 full_name=f"Owner {prefix}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv, terms_accepted_at=datetime.utcnow())
    owner.set_password("pw12345678")
    db.session.add(owner); db.session.commit()

    company_ids = []
    for i in range(count):
        c = Company(name=f"__{prefix}__{i}", base_currency="EGP",
                    subdomain=f"{prefix.lower()}{i}", plan_id=plan.id,
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1))
        db.session.add(c); db.session.commit()
        seed_default_coa(c.id); db.session.commit()
        db.session.execute(user_companies.insert().values(
            user_id=owner.id, company_id=c.id, role="owner"))
        company_ids.append(c.id)
    db.session.commit()
    return owner.email, owner.id, company_ids


def _authed_client(app, uid, cid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
        s["_fresh"] = True
        s["active_company_id"] = cid
    return c


def _post_je(cid, *, debit_code, credit_code, amount, when=None):
    """Post one Dr/Cr JE. Returns the JournalEntry."""
    from app import db
    from app.models import Account
    from app.services.ledger import post_journal
    da = Account.query.filter_by(company_id=cid, code=debit_code).first()
    ca = Account.query.filter_by(company_id=cid, code=credit_code).first()
    assert da and ca, f"missing accounts {debit_code}/{credit_code}"
    return post_journal(
        company_id=cid, description="audit",
        lines=[
            {"account_id": da.id, "debit": amount, "credit": 0},
            {"account_id": ca.id, "debit": 0, "credit": amount},
        ],
        entry_date=when or date.today(),
    )


@check("1. /companies/<id>/branches route registered")
def _():
    from app import create_app
    app = create_app()
    names = {r.endpoint for r in app.url_map.iter_rules()}
    assert "companies.branches" in names, "missing endpoint"
    return "companies.branches present"


@check("2. two consolidated report routes registered")
def _():
    from app import create_app
    app = create_app()
    names = {r.endpoint for r in app.url_map.iter_rules()}
    for want in ("reports.consolidated_income",
                 "reports.consolidated_balance"):
        assert want in names, f"missing {want}"
    return "consolidated_income + consolidated_balance"


@check("3. link a branch → parent_id set + shows in list")
def _():
    from app import create_app, db
    from app.models import Company
    app = create_app()
    with app.app_context():
        email, uid, cids = _boot_two_owned_companies("BR3")
        try:
            parent_id, branch_id = cids
            client = _authed_client(app, uid, parent_id)
            r = client.post(
                f"/companies/{parent_id}/branches",
                data={"action": "link",
                       "branch_company_id": branch_id},
            )
            assert r.status_code in (302, 303)
            branch = db.session.get(Company, branch_id)
            db.session.refresh(branch)
            assert branch.parent_id == parent_id, \
                f"parent_id={branch.parent_id}"
            return "branch linked, parent_id set"
        finally:
            pass


@check("4. cross-tenant link refused")
def _():
    from app import create_app, db
    from app.models import Company
    app = create_app()
    with app.app_context():
        # Owner A owns 1 company; Owner B owns 1 company. A tries to
        # link B's company as a branch of A's.
        email_a, uid_a, cids_a = _boot_two_owned_companies("BR4A",
                                                              count=1)
        try:
            email_b, uid_b, cids_b = _boot_two_owned_companies("BX4B",
                                                                  count=1)
            parent_id = cids_a[0]
            stranger_id = cids_b[0]
            client = _authed_client(app, uid_a, parent_id)
            r = client.post(
                f"/companies/{parent_id}/branches",
                data={"action": "link",
                       "branch_company_id": stranger_id},
            )
            assert r.status_code in (302, 303)
            stranger = db.session.get(Company, stranger_id)
            db.session.refresh(stranger)
            assert stranger.parent_id is None, \
                "cross-tenant link succeeded!"
            return "cross-tenant refused"
        finally:
            pass


@check("5. multi-level hierarchy refused")
def _():
    from app import create_app, db
    from app.models import Company
    app = create_app()
    with app.app_context():
        email, uid, cids = _boot_two_owned_companies("BR5", count=3)
        try:
            a, b, c = cids
            # First link b under a — legal
            client = _authed_client(app, uid, a)
            r = client.post(
                f"/companies/{a}/branches",
                data={"action": "link", "branch_company_id": b},
            )
            assert r.status_code in (302, 303)
            b_obj = db.session.get(Company, b)
            db.session.refresh(b_obj)
            assert b_obj.parent_id == a
            # Try to link c under b — refuse (b already a branch)
            client_b = _authed_client(app, uid, b)
            r = client_b.post(
                f"/companies/{b}/branches",
                data={"action": "link", "branch_company_id": c},
            )
            # branch mode → only unlink accepted, link ignored
            c_obj = db.session.get(Company, c)
            db.session.refresh(c_obj)
            assert c_obj.parent_id is None, \
                "hierarchy leaked — c linked under b (already a branch)"
            return "multi-level refused"
        finally:
            pass


@check("6. consolidated_income_statement sums per-branch numbers")
def _():
    from app import create_app, db
    from app.models import Company
    from app.services.reports import (
        income_statement, consolidated_income_statement,
    )
    app = create_app()
    with app.app_context():
        email, uid, cids = _boot_two_owned_companies("BR6", count=2)
        try:
            parent_id, branch_id = cids
            # Link branch under parent
            b = db.session.get(Company, branch_id)
            b.parent_id = parent_id
            db.session.commit()
            # Seed: parent has 500 revenue (Cr 4100) received in 1110.
            # branch has 300 revenue.
            _post_je(parent_id, debit_code="1110",
                      credit_code="4100", amount=500)
            _post_je(branch_id, debit_code="1110",
                      credit_code="4100", amount=300)
            data = consolidated_income_statement(parent_id)
            assert abs(data["total_revenue"] - 800) < 0.01, \
                f"expected 800, got {data['total_revenue']}"
            # Per-branch drill-down carries individual totals
            assert data["per_branch"][parent_id]["data"]["total_revenue"] == 500
            assert data["per_branch"][branch_id]["data"]["total_revenue"] == 300
            return "500 + 300 = 800 (matches per-branch drill-down)"
        finally:
            pass


@check("7. unlink → parent_id=None + branch data intact")
def _():
    from app import create_app, db
    from app.models import Company, JournalEntry
    app = create_app()
    with app.app_context():
        email, uid, cids = _boot_two_owned_companies("BR7", count=2)
        try:
            parent_id, branch_id = cids
            branch = db.session.get(Company, branch_id)
            branch.parent_id = parent_id
            db.session.commit()
            _post_je(branch_id, debit_code="1110",
                      credit_code="4100", amount=200)
            je_before = JournalEntry.query.filter_by(
                company_id=branch_id).count()

            client = _authed_client(app, uid, branch_id)
            r = client.post(
                f"/companies/{branch_id}/branches",
                data={"action": "unlink"},
            )
            assert r.status_code in (302, 303)
            db.session.refresh(branch)
            assert branch.parent_id is None
            je_after = JournalEntry.query.filter_by(
                company_id=branch_id).count()
            assert je_before == je_after, \
                f"branch data disturbed: {je_before} → {je_after}"
            return f"unlinked; JE count unchanged ({je_after})"
        finally:
            pass


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
