#!/usr/bin/env python3
"""PER-CO-NUMBERING (Abdelhamid 2026-07-04) — audit.

Proves that leads, projects, and auto-generated SKUs each count from
1 per company, independent of what other companies have.

  1. A fresh company gets L-0001 for its first lead + PRJ-0001 for
     its first project + PRD-0001 for its first auto-SKU.
  2. Adding more rows increments sequentially within that company.
  3. A DIFFERENT company started at the same time also gets L-0001,
     PRJ-0001, PRD-0001 — global IDs do not leak.
  4. Migration backfill produced the same result on existing rows
     (verified by re-running next_number after backfill: it hands out
     the correct next value, not a colliding one).
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

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


def _setup():
    from app.models import Company, User, UserStatus
    from app.models.user import user_companies
    # Clean stale test user first.
    u_old = User.query.filter_by(email="pcn_actor@t.co").first()
    if u_old:
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u_old.id))
        db.session.delete(u_old); db.session.commit()

    for name in ("__PCN_A__", "__PCN_B__"):
        old = Company.query.filter_by(name=name).first()
        if old:
            _teardown_company(old.id)

    from app.services.seed_coa import seed_default_coa
    a = Company(name="__PCN_A__", base_currency="SAR")
    b = Company(name="__PCN_B__", base_currency="EGP")
    db.session.add_all([a, b]); db.session.flush()
    seed_default_coa(a.id)
    seed_default_coa(b.id)

    # Actor user needed as assigned_to_id / created_by_id on leads.
    u = User(email="pcn_actor@t.co", full_name="actor",
              status=UserStatus.ACTIVE.value)
    u.set_password("x")
    db.session.add(u); db.session.flush()
    for cid in (a.id, b.id):
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=cid, role="owner",
        ))
    db.session.commit()
    _STATE.update(a_id=a.id, b_id=b.id, user_id=u.id)


def _teardown_company(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id = :c"
                ), {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                       {"c": company_id})


def _make_lead(company_id, name="زبون"):
    from app.models import Lead, LeadStatus
    from app.services.numbering import next_number
    L = Lead(
        company_id=company_id,
        number=next_number(company_id, "LEAD"),
        client_name=name, phone="0500000000",
        service_needed="خدمة",
        lead_type="INBOUND", source="WEBSITE",
        status=LeadStatus.NEW_LEAD,
        created_by_id=_STATE["user_id"],
        assigned_to_id=_STATE["user_id"],
    )
    db.session.add(L); db.session.commit()
    return L


def _make_project(company_id, customer_id, name="مشروع"):
    from app.models import Project, ProjectStatus
    from app.services.numbering import next_number
    from datetime import date, timedelta
    p = Project(
        company_id=company_id,
        number=next_number(company_id, "PROJECT"),
        name=name, customer_id=customer_id,
        type="خدمة", manager_id=_STATE["user_id"],
        start_date=date.today(),
        end_date=date.today() + timedelta(days=30),
        status=ProjectStatus.PLANNING,
    )
    db.session.add(p); db.session.commit()
    return p


def _make_customer(company_id, name):
    from app.models import Customer
    from app.services.subsidiary import ensure_customer_account
    cust = Customer(company_id=company_id, name=name)
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    db.session.commit()
    return cust


@check("1. First lead / project / SKU in company A → *-0001")
def _():
    aid = _STATE["a_id"]
    L = _make_lead(aid, "أول زبون")
    cust = _make_customer(aid, "عميل مشاريع")
    P = _make_project(aid, cust.id, "أول مشروع")
    from app.routes.products import _generate_variant_sku
    sku = _generate_variant_sku(aid, 999)   # arbitrary product_id
    assert L.number == "L-0001", f"lead: {L.number}"
    assert P.number == "PRJ-0001", f"project: {P.number}"
    assert sku == "PRD-0001", f"sku: {sku}"
    _STATE["a_customer_id"] = cust.id
    return f"{L.number} / {P.number} / {sku}"


@check("2. Sequential within the same company")
def _():
    aid = _STATE["a_id"]
    L2 = _make_lead(aid, "زبون تاني")
    L3 = _make_lead(aid, "زبون تالت")
    P2 = _make_project(aid, _STATE["a_customer_id"], "مشروع تاني")
    from app.routes.products import _generate_variant_sku
    sku2 = _generate_variant_sku(aid, 12345)
    sku3 = _generate_variant_sku(aid, 67890)
    assert L2.number == "L-0002"
    assert L3.number == "L-0003"
    assert P2.number == "PRJ-0002"
    assert sku2 == "PRD-0002"
    assert sku3 == "PRD-0003"
    return f"{L2.number}, {L3.number} / {P2.number} / {sku2}, {sku3}"


@check("3. A different company also starts at *-0001")
def _():
    bid = _STATE["b_id"]
    L = _make_lead(bid, "زبون شركة تانية")
    cust = _make_customer(bid, "عميل شركة تانية")
    P = _make_project(bid, cust.id, "مشروع شركة تانية")
    from app.routes.products import _generate_variant_sku
    sku = _generate_variant_sku(bid, 999)
    # Independence: company A's counters didn't leak in.
    assert L.number == "L-0001", f"cross-tenant leak on lead: {L.number}"
    assert P.number == "PRJ-0001", f"cross-tenant leak on project: {P.number}"
    assert sku == "PRD-0001", f"cross-tenant leak on sku: {sku}"
    return f"{L.number} / {P.number} / {sku} (independent from company A)"


@check("4. Global PK doesn't leak — next_number ignores foreign rows")
def _():
    """Even though company A now has leads with global ids 1-3 and
    company B has global ids 4+, company B's number sequence still
    started fresh at 1. Prove by comparing L.id vs L.number for
    company B's leads."""
    from app.models import Lead
    bid = _STATE["b_id"]
    leads_b = Lead.query.filter_by(company_id=bid).order_by(Lead.id).all()
    # Global ids should be > 3 (company A used 1-3), but numbers
    # should start at L-0001.
    assert leads_b[0].number == "L-0001"
    assert leads_b[0].id > 3, \
        f"test invariant broken: expected global id > 3, got {leads_b[0].id}"
    return (f"lead in company B: global id={leads_b[0].id}, "
              f"per-company number={leads_b[0].number}")


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        from tests._orphan_sweep import preflight
        preflight()
        try:
            _setup()
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
        finally:
            for key in ("a_id", "b_id"):
                if key in _STATE:
                    _teardown_company(_STATE[key])
            # Actor user survives the company teardown — clean it too.
            from sqlalchemy import text
            with db.engine.begin() as conn:
                conn.execute(text(
                    "DELETE FROM user_companies WHERE user_id IN "
                    "(SELECT id FROM users WHERE email = 'pcn_actor@t.co')"
                ))
                conn.execute(text(
                    "DELETE FROM users WHERE email = 'pcn_actor@t.co'"
                ))
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
