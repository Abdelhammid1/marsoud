#!/usr/bin/env python3
"""MARSOUD-SAAS-CUSTOMER-PHONE (2026-08-18) — audit.

Verifies:
  A. `ensure_saas_customer` creates a Customer with the phone
     from `company.phone` (preferred) or the owner's `User.phone`
     (fallback).
  B. Existing Customer with phone=NULL gets healed on the next
     `ensure_saas_customer` call.
  C. Existing Customer with a manually-set phone is NEVER
     overwritten (idempotence + super-admin edits preserved).
  D. Company with NO company.phone AND owner with NO user.phone
     → Customer.phone stays NULL (no bogus fill).
  E. The backfill migration `n1o4p7q0r3s6` correctly populates
     legacy rows via the `companies.saas_customer_id` FK.
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402


CHECKS = []
CO_NAME = "__SAAS_PHONE_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text
    from app.models import Company, User, Customer
    from app.models.user import user_companies

    # Unlink saas_customer_id first (FK), then delete customers,
    # then delete companies + users. Best-effort.
    try:
        db.session.execute(text(
            "UPDATE companies SET saas_customer_id = NULL "
            "WHERE name LIKE :p"), {"p": f"{CO_NAME}%"})
        db.session.commit()
    except Exception:
        db.session.rollback()

    # Purge fixture customers
    Customer.query.filter(Customer.name.like(f"{CO_NAME}%")).delete(
        synchronize_session=False)
    db.session.commit()

    ids = [c.id for c in Company.query.filter(
        Company.name.like(f"{CO_NAME}%")).all()]
    if ids:
        for t in reversed(db.metadata.sorted_tables):
            if "company_id" in t.c:
                try:
                    db.session.execute(
                        t.delete().where(t.c.company_id.in_(ids)))
                except Exception:
                    db.session.rollback()
        db.session.commit()
    for u in User.query.filter(
            User.email.like(f"{CO_NAME.lower()}%@x.local")).all():
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id))
        db.session.delete(u)
    db.session.commit()
    for cid in ids:
        try:
            db.session.execute(
                text("DELETE FROM companies WHERE id = :i"),
                {"i": cid})
        except Exception:
            db.session.rollback()
    db.session.commit()


def _mk_company(name, company_phone=None, owner_phone=None):
    """Create a fresh tenant Company + owner user linked via
    user_companies. Returns the Company."""
    from app.models import Company, User
    from app.models.user import user_companies
    from app.services.legal import get_terms_version

    tv = get_terms_version()
    now = datetime.utcnow()
    u = User(email=f"{CO_NAME.lower()}_{name.lower()}@x.local",
             full_name=f"Owner {name}",
             phone=owner_phone,
             terms_version=tv, terms_accepted_at=now)
    u.set_password("Passw0rd!audit1")
    db.session.add(u); db.session.flush()
    co = Company(name=f"{CO_NAME}_{name}",
                 base_currency="EGP",
                 phone=company_phone)
    db.session.add(co); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=co.id, role="owner"))
    db.session.commit()
    return co


# ─── A. Create with phone from company / owner ────────────────────────
@check("A1: ensure_saas_customer creates Customer with company.phone")
def A1():
    from app.services.saas_billing import ensure_saas_customer
    co = _mk_company("A1", company_phone="+201111111111",
                     owner_phone="+202222222222")
    cust = ensure_saas_customer(co)
    db.session.commit()
    assert cust.phone == "+201111111111", (
        f"company.phone should win: got {cust.phone!r}")


@check("A2: ensure_saas_customer falls back to owner.phone")
def A2():
    from app.services.saas_billing import ensure_saas_customer
    co = _mk_company("A2", company_phone=None,
                     owner_phone="+203333333333")
    cust = ensure_saas_customer(co)
    db.session.commit()
    assert cust.phone == "+203333333333", (
        f"owner.phone fallback: got {cust.phone!r}")


# ─── B. Reuse-branch heals NULL ───────────────────────────────────────
@check("B1: existing Customer with NULL phone gets healed on next call")
def B1():
    from app.services.saas_billing import ensure_saas_customer
    from app.models import Customer
    co = _mk_company("B1", company_phone="+204444444444",
                     owner_phone=None)
    # Simulate legacy state — call once, then null-out the phone.
    cust = ensure_saas_customer(co)
    db.session.commit()
    cust.phone = None
    db.session.commit()
    # Next call must heal it.
    cust2 = ensure_saas_customer(co)
    db.session.commit()
    assert cust2.id == cust.id, "should be the same row"
    assert cust2.phone == "+204444444444", (
        f"heal branch failed: {cust2.phone!r}")


# ─── C. Never overwrite a manually-set phone ──────────────────────────
@check("C1: manually-set phone is NEVER overwritten")
def C1():
    from app.services.saas_billing import ensure_saas_customer
    co = _mk_company("C1", company_phone="+205555555555",
                     owner_phone=None)
    cust = ensure_saas_customer(co)
    db.session.commit()
    # Super-admin overrides
    cust.phone = "+206666666666"
    db.session.commit()
    # Change company.phone underneath — reuse should NOT clobber
    # the manually-set value.
    co.phone = "+207777777777"
    db.session.commit()
    cust2 = ensure_saas_customer(co)
    db.session.commit()
    assert cust2.phone == "+206666666666", (
        f"manually-set phone overwritten: {cust2.phone!r}")


# ─── D. No bogus fill when both sources are NULL ──────────────────────
@check("D1: both sources NULL → Customer.phone stays NULL")
def D1():
    from app.services.saas_billing import ensure_saas_customer
    co = _mk_company("D1", company_phone=None, owner_phone=None)
    cust = ensure_saas_customer(co)
    db.session.commit()
    assert cust.phone is None, (
        f"bogus phone filled from nothing: {cust.phone!r}")


# ─── E. Backfill migration (via direct SQL simulate) ──────────────────
@check("E1: backfill SQL populates NULL customers.phone via saas_customer_id FK")
def E1():
    from sqlalchemy import text
    from app.services.saas_billing import ensure_saas_customer
    from app.models import Customer
    # Set up a legacy-shaped row: create the Customer, then null
    # its phone directly (bypassing the ensure_ helper's heal).
    co = _mk_company("E1", company_phone="+208888888888",
                     owner_phone="+209999999999")
    cust = ensure_saas_customer(co)
    db.session.commit()
    db.session.execute(text(
        "UPDATE customers SET phone = NULL WHERE id = :i"),
        {"i": cust.id})
    db.session.commit()
    # Sanity check: phone is really NULL
    row = db.session.execute(text(
        "SELECT phone FROM customers WHERE id = :i"),
        {"i": cust.id}).fetchone()
    assert row[0] is None

    # Now run the backfill SQL (same shape as the migration).
    db.session.execute(text("""
        UPDATE customers
           SET phone = (
                 SELECT COALESCE(
                          co.phone,
                          (SELECT u.phone
                             FROM users u
                             JOIN user_companies uc
                               ON uc.user_id = u.id
                            WHERE uc.company_id = co.id
                            ORDER BY (CASE
                                      WHEN uc.role='owner' THEN 0
                                      WHEN uc.role='admin' THEN 1
                                      ELSE 2 END), u.id
                            LIMIT 1))
                   FROM companies co
                  WHERE co.saas_customer_id = customers.id
                  LIMIT 1)
         WHERE customers.phone IS NULL
           AND EXISTS (
                 SELECT 1 FROM companies co
                  WHERE co.saas_customer_id = customers.id
                    AND (co.phone IS NOT NULL
                         OR EXISTS (
                              SELECT 1 FROM users u
                                JOIN user_companies uc
                                  ON uc.user_id = u.id
                               WHERE uc.company_id = co.id
                                 AND u.phone IS NOT NULL)))
    """))
    db.session.commit()
    row = db.session.execute(text(
        "SELECT phone FROM customers WHERE id = :i"),
        {"i": cust.id}).fetchone()
    assert row[0] == "+208888888888", (
        f"backfill failed: got {row[0]!r}")


# ─── Runner ───────────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    with app.app_context():
        _teardown()
        try:
            failed = []
            for label, fn in CHECKS:
                try:
                    fn()
                    print(f"  [OK]   {label}")
                except Exception as e:
                    failed.append((label, e))
                    print(f"  [FAIL] {label}\n         -> {e}")
            total = len(CHECKS)
            ok = total - len(failed)
            print()
            print(f"{ok}/{total} OK" if not failed
                  else f"{ok}/{total} -- {len(failed)} FAILED")
            return 0 if not failed else 1
        finally:
            _teardown()


if __name__ == "__main__":
    sys.exit(main())
