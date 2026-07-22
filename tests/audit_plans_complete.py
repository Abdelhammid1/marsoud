#!/usr/bin/env python3
"""MARSOUD-PLANS-COMPLETE (Abdelhamid 2026-07-22) — audit.

Checks:
  1. `flask seed-plans` produces the 3 canonical plans (Starter,
     Growth, Pro) at the exact EGP prices.
  2. Rerunning the seed is a no-op — no duplicate rows, quotas
     stay put.
  3. Each seeded plan has 3 Quota rows (USERS, AI_TOKENS_MONTH,
     STORAGE_BYTES) at the ticket's values.
  4. Legacy plans (retail, services) get deactivated when no
     company is bound. Guard refuses to deactivate if bound.
  5. `compute_overage` returns nothing when usage is within
     limits.
  6. `compute_overage` charges for USERS overage at the plan's
     unit price.
  7. Storage upload above the limit raises UserFileError with the
     Arabic quota message.
  8. `/settings/usage` renders for the owner + shows the bars.
"""
import io
import os
import sys
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        target_cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__PC_%__'"))]
        for cid in target_cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'pc-%@x.test'"))
        # Prior-run plans left behind by a mid-check failure — kill
        # any quota row pointing at them BEFORE the plan row so the
        # unique-constraint replay in the next run doesn't stumble.
        conn.execute(text(
            "DELETE FROM quotas WHERE plan_id IN "
            "(SELECT id FROM plans WHERE code = 'pc-tight')"))
        conn.execute(text(
            "DELETE FROM plans WHERE code = 'pc-tight'"))
        # SQLite reuses primary keys → a new plan getting the id of a
        # previously-deleted 'pc-tight' would collide with any orphan
        # quota row that survived. Sweep orphans.
        conn.execute(text(
            "DELETE FROM quotas WHERE plan_id NOT IN "
            "(SELECT id FROM plans)"))
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id NOT IN "
                    "(SELECT id FROM companies)"))


def _run_seed():
    from click.testing import CliRunner
    from app.cli import seed_plans_command
    from flask import current_app
    runner = CliRunner()
    # Invoke inside the current app context — Click's runner would
    # otherwise not see the app.
    with current_app.test_request_context():
        result = runner.invoke(seed_plans_command)
    return result


@check("1. seed-plans creates Starter/Growth/Pro at ticket prices")
def _():
    from app.models import Plan
    _teardown()
    result = _run_seed()
    assert result.exit_code == 0, \
        f"seed failed: {result.output}\n{result.exception}"
    starter = Plan.query.filter_by(code="starter").first()
    growth = Plan.query.filter_by(code="growth").first()
    pro = Plan.query.filter_by(code="pro").first()
    assert starter and starter.price_monthly == 799
    assert growth and growth.price_monthly == 1499
    assert pro and pro.price_monthly == 2799
    _STATE["starter_id"] = starter.id
    _STATE["growth_id"] = growth.id
    _STATE["pro_id"] = pro.id
    return "3 plans present at 799/1499/2799 EGP"


@check("2. seed-plans is idempotent (rerun is a no-op)")
def _():
    from app.models import Plan
    before = Plan.query.count()
    _run_seed()
    after = Plan.query.count()
    assert before == after, f"plan count changed: {before} → {after}"
    return f"plan count stable at {after}"


@check("3. Each seeded plan has 3 Quota rows")
def _():
    from app.models import (
        Quota, QUOTA_USERS, QUOTA_AI_TOKENS_MONTH, QUOTA_STORAGE_BYTES,
    )
    for code, expected_users, expected_tokens, expected_gb in (
        ("starter", 3, 300_000, 2),
        ("growth", 7, 600_000, 10),
        ("pro", 15, 1_000_000, 50),
    ):
        pid = _STATE[f"{code}_id"]
        rows = {q.quota_type: q for q in Quota.query.filter_by(
            plan_id=pid).all()}
        assert QUOTA_USERS in rows, f"{code} missing USERS quota"
        assert QUOTA_AI_TOKENS_MONTH in rows, \
            f"{code} missing AI_TOKENS quota"
        assert QUOTA_STORAGE_BYTES in rows, \
            f"{code} missing STORAGE quota"
        assert rows[QUOTA_USERS].included_amount == expected_users
        assert rows[QUOTA_AI_TOKENS_MONTH].included_amount == \
            expected_tokens
        assert rows[QUOTA_STORAGE_BYTES].included_amount == \
            expected_gb * (1024**3)
    return "9 quota rows across the 3 plans"


@check("4. Legacy retail/services deactivated when unbound, guarded when bound")
def _():
    from app.models import Plan, Company
    # Ensure both exist (they may or may not — seed a stub if missing).
    for code in ("retail", "services"):
        if not Plan.query.filter_by(code=code).first():
            db.session.add(Plan(
                code=code, name=code, name_ar=code,
                is_active=True, price_monthly=0))
    db.session.commit()

    # Bind a company to 'retail' — the guard should refuse.
    retail = Plan.query.filter_by(code="retail").first()
    retail.is_active = True
    db.session.add(retail); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    c = Company(name="__PC_RETAIL_BOUND__", base_currency="EGP",
                 subdomain="pc-retail", plan_id=retail.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    db.session.commit()

    _run_seed()
    retail = Plan.query.filter_by(code="retail").first()
    assert retail.is_active, "guard failed: retail deactivated despite bound company"

    # Unbind, rerun — should deactivate.
    c.plan_id = None
    c.intended_plan_id = None
    db.session.commit()
    _run_seed()
    retail = Plan.query.filter_by(code="retail").first()
    services = Plan.query.filter_by(code="services").first()
    assert not retail.is_active, "retail should be deactivated"
    assert not services.is_active, "services should be deactivated"
    return "guard OK + unbind path deactivates both"


@check("5. compute_overage returns empty when within limits")
def _():
    from app.models import Company
    from app.services.quotas import compute_overage
    from app.services.seed_coa import seed_default_coa
    c = Company(name="__PC_UNDER__", base_currency="EGP",
                 subdomain="pc-under", plan_id=_STATE["starter_id"],
                 intended_plan_id=_STATE["starter_id"])
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    db.session.commit()
    overage = compute_overage(c)
    assert overage == {} or all(row["extra"] == 0
                                  for row in overage.values()), \
        f"expected no overage, got {overage}"
    return "no overage row for company under limits"


@check("6. compute_overage charges for USERS overage at unit price")
def _():
    from app.models import (
        Company, User, UserStatus, QUOTA_USERS,
    )
    from app.models.user import user_companies
    from app.services.quotas import compute_overage
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash

    # Starter includes 3 users; add 5 → 2 overage → 2 × 150 = 300 EGP.
    c = Company(name="__PC_OVER__", base_currency="EGP",
                 subdomain="pc-over", plan_id=_STATE["starter_id"],
                 intended_plan_id=_STATE["starter_id"])
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    db.session.flush()
    for i in range(5):
        u = User(email=f"pc-over-{i}@x.test",
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name=f"pc-over-{i}", is_active=True,
                 status=UserStatus.ACTIVE.value)
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()

    overage = compute_overage(c)
    row = overage.get(QUOTA_USERS)
    assert row, f"expected USERS overage row, got {overage}"
    assert row["extra"] == 2, f"expected 2 extra users, got {row}"
    assert row["amount"] == 300.0, \
        f"expected 300 EGP (2 × 150), got {row['amount']}"
    return f"USERS overage: 5/3 → +2 → {row['amount']} EGP"


@check("7. Storage upload above limit raises UserFileError")
def _():
    from app.models import (
        Company, Quota, QUOTA_STORAGE_BYTES, ENF_BLOCK,
    )
    from app.services.user_files import save_user_file, UserFileError
    from app.services.seed_coa import seed_default_coa
    # Fresh plan sized to 100 bytes so a small upload trips it.
    from app.models import Plan
    p = Plan(code="pc-tight", name="Tight", name_ar="ضيّق",
             is_active=True, price_monthly=0)
    db.session.add(p); db.session.flush()
    db.session.add(Quota(
        plan_id=p.id, quota_type=QUOTA_STORAGE_BYTES,
        included_amount=100, enforcement_mode=ENF_BLOCK,
        price_per_extra_unit=0))
    db.session.flush()
    c = Company(name="__PC_TIGHT__", base_currency="EGP",
                 subdomain="pc-tight", plan_id=p.id,
                 intended_plan_id=p.id)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    from werkzeug.security import generate_password_hash
    from app.models import User, UserStatus
    from app.models.user import user_companies
    u = User(email="pc-tight-owner@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="pc-tight-owner", is_active=True,
             status=UserStatus.ACTIVE.value)
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()

    # Upload a 200-byte fake pdf → over 100-byte limit.
    from werkzeug.datastructures import FileStorage
    fs = FileStorage(stream=io.BytesIO(b"x" * 200),
                      filename="over.pdf",
                      content_type="application/pdf")
    raised = False
    try:
        save_user_file(company_id=c.id, user_id=u.id, file_storage=fs)
    except UserFileError as e:
        raised = True
        _STATE["storage_err"] = str(e)
    assert raised, "expected UserFileError for over-quota upload"
    return f"blocked: {_STATE['storage_err'][:60]}"


@check("8. /settings/usage renders for owner")
def _():
    from flask import current_app, g
    from app.models import (
        Company, User, UserStatus, Plan,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash

    c = Company(name="__PC_DASH__", base_currency="EGP",
                 subdomain="pc-dash", plan_id=_STATE["starter_id"],
                 intended_plan_id=_STATE["starter_id"])
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    u = User(email="pc-dash-owner@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name="pc-dash-owner", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=__import__("datetime").datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.commit()

    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(u.id)
        sess["_fresh"] = True
        sess["active_company_id"] = c.id
    r = client.get("/settings/usage/")
    assert r.status_code == 200, f"got {r.status_code}"
    body = r.get_data(as_text=True)
    assert "استهلاك الباقة" in body
    assert "المستخدمون" in body
    return "usage page rendered"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _teardown()
            for label, fn in CHECKS:
                try:
                    res = fn()
                    print(f"PASS  {label}  ⇒ {res}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback; traceback.print_exc()
        finally:
            _teardown()
            from sqlalchemy import text
            with db.engine.begin() as conn:
                conn.execute(text(
                    "DELETE FROM plans WHERE code = 'pc-tight'"))
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
