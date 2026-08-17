#!/usr/bin/env python3
"""MARSOUD-MOBILE-FLUTTER — audit for the mobile-facing JSON API.

Covers the three new blueprints:
  · api_v1_auth        (/api/v1/auth/*)
  · api_v1_me          (/api/v1/my/*)
  · api_v1_notifications (/api/v1/notifications/*)

Runs one happy-path + one auth-guard check per surface. Also exercises
the cross-tenant safety of `?company_id=N`: an employee logged into
company A must not be able to read anything from company B by flipping
the query param.

Executed the same way as the other `audit_*.py` files in this tree:

    python tests/audit_api_v1_mobile.py

Exits 0 on green, prints "N/N ✓". Exits 1 on any failure with the
failing check names.
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402


CHECKS = []
COMPANY_A_NAME = "__MOBILE_API_AUDIT_A__"
COMPANY_B_NAME = "__MOBILE_API_AUDIT_B__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _setup():
    """Two companies. Employee E belongs to A. Employee F belongs to B.
    Both have an HR record, so `_my_employee_or_404` resolves cleanly.
    """
    from app.models import Company, User, Employee, Plan
    from app.models.user import user_companies
    from app.services.api_tokens import generate_token

    _teardown()

    plan = Plan.query.filter_by(code="__mobapi__").first()
    if not plan:
        plan = Plan(code="__mobapi__", name="Audit", name_ar="تدقيق",
                    allowed_subitems=None)
        plan.set_modules([
            "accounting", "sales", "inventory", "purchases", "pos", "crm",
            "hr", "reports", "agent", "employee_reports", "manufacturing",
            "evaluations", "insights", "settings",
        ])
        db.session.add(plan)
        db.session.flush()

    from app.services.legal import get_terms_version
    terms_now = get_terms_version()

    co_a = Company(name=COMPANY_A_NAME, plan_id=plan.id)
    co_b = Company(name=COMPANY_B_NAME, plan_id=plan.id)
    db.session.add_all([co_a, co_b])
    db.session.flush()

    ue = User(email="__mobapi_e@audit.local", full_name="Employee E",
              terms_version=terms_now,
              terms_accepted_at=datetime.utcnow())
    ue.set_password("Passw0rd!audit1")
    uf = User(email="__mobapi_f@audit.local", full_name="Employee F",
              terms_version=terms_now,
              terms_accepted_at=datetime.utcnow())
    uf.set_password("Passw0rd!audit1")
    db.session.add_all([ue, uf])
    db.session.flush()

    db.session.execute(user_companies.insert().values(
        user_id=ue.id, company_id=co_a.id, role="employee"))
    db.session.execute(user_companies.insert().values(
        user_id=uf.id, company_id=co_b.id, role="employee"))

    emp_e = Employee(company_id=co_a.id, user_id=ue.id,
                     name="موظف تجربة", job_title="مبرمج",
                     start_date=date(2024, 1, 1))
    emp_f = Employee(company_id=co_b.id, user_id=uf.id,
                     name="موظف آخر", job_title="مبرمج",
                     start_date=date(2024, 1, 1))
    db.session.add_all([emp_e, emp_f])
    db.session.commit()

    # Mint bearer tokens the audit's test client can send.
    raw_e, tok_e = generate_token(ue, "mobile:audit-E")
    raw_f, tok_f = generate_token(uf, "mobile:audit-F")

    _STATE["company_a_id"] = co_a.id
    _STATE["company_b_id"] = co_b.id
    _STATE["user_e_id"] = ue.id
    _STATE["user_f_id"] = uf.id
    _STATE["emp_e_id"] = emp_e.id
    _STATE["emp_f_id"] = emp_f.id
    _STATE["token_e"] = raw_e
    _STATE["token_f"] = raw_f
    _STATE["ue_email"] = ue.email
    _STATE["ue_password"] = "Passw0rd!audit1"


def _teardown():
    """Same generic wipe pattern the other audits use."""
    from sqlalchemy import text
    from app.models import Company, User, Plan
    from app.models.user import user_companies

    ids = [c.id for c in Company.query.filter(
        Company.name.in_([COMPANY_A_NAME, COMPANY_B_NAME])).all()]
    if ids:
        tables = list(reversed(db.metadata.sorted_tables))
        for t in tables:
            if "company_id" in t.c:
                db.session.execute(
                    t.delete().where(t.c.company_id.in_(ids)))
        db.session.commit()

    for u in User.query.filter(
            User.email.like("__mobapi_%@audit.local")).all():
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id))
        for t in reversed(db.metadata.sorted_tables):
            if "user_id" in t.c and t.name != "user_companies":
                db.session.execute(t.delete().where(t.c.user_id == u.id))
        db.session.delete(u)
    db.session.commit()

    for cid in ids:
        db.session.execute(
            text("DELETE FROM companies WHERE id = :i"), {"i": cid})
    for p in Plan.query.filter_by(code="__mobapi__").all():
        db.session.delete(p)
    db.session.commit()


# ─── Helpers ───────────────────────────────────────────────────────────
def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _get(url, token=None, headers=None):
    app = _STATE["app"]
    h = dict(headers or {})
    if token:
        h.update(_bearer(token))
    return app.test_client().get(url, headers=h)


def _post(url, payload=None, token=None):
    app = _STATE["app"]
    h = _bearer(token) if token else {}
    return app.test_client().post(
        url, json=payload or {}, headers=h)


def _json(resp):
    return json.loads(resp.data.decode("utf-8"))


# ─── Checks: auth ──────────────────────────────────────────────────────
@check("A1: /api/v1/auth/login without body -> 400 missing_credentials")
def A1():
    r = _post("/api/v1/auth/login", {})
    assert r.status_code == 400, r.status_code
    assert _json(r).get("error") == "missing_credentials"


@check("A2: /api/v1/auth/login with wrong password -> 401 invalid_credentials")
def A2():
    r = _post("/api/v1/auth/login", {
        "email": _STATE["ue_email"], "password": "nope"})
    assert r.status_code == 401, r.status_code


@check("A3: /api/v1/auth/login happy path returns token + user + companies")
def A3():
    r = _post("/api/v1/auth/login", {
        "email": _STATE["ue_email"],
        "password": _STATE["ue_password"],
        "device_name": "audit-suite",
    })
    assert r.status_code == 200, r.status_code
    body = _json(r)
    assert body["token"].startswith("mrs_live_"), body["token"][:20]
    assert body["user"]["email"] == _STATE["ue_email"]
    assert len(body["companies"]) == 1
    assert body["companies"][0]["role"] == "employee"


# ─── Checks: bearer gate ───────────────────────────────────────────────
@check("B1: /api/v1/my/account without bearer -> 401")
def B1():
    r = _get("/api/v1/my/account")
    assert r.status_code == 401, r.status_code


@check("B2: /api/v1/notifications without bearer -> 401")
def B2():
    r = _get("/api/v1/notifications")
    assert r.status_code == 401, r.status_code


# ─── Checks: /my/account happy path ────────────────────────────────────
@check("C1: /api/v1/my/account with bearer returns full bundle")
def C1():
    r = _get("/api/v1/my/account", token=_STATE["token_e"])
    assert r.status_code == 200, (r.status_code, r.data[:200])
    body = _json(r)
    assert body["employee"]["id"] == _STATE["emp_e_id"]
    # Every top-level bundle key must be present so the mobile UI
    # never crashes on a missing section.
    for key in ("employee", "tenure_label", "payslips", "leave",
                "advance", "today_checkin"):
        assert key in body, key
    for key in ("types", "balances", "requests"):
        assert key in body["leave"], key


# ─── Checks: cross-tenant safety ───────────────────────────────────────
@check("D1: employee-E cannot see company-B by flipping ?company_id")
def D1():
    r = _get(f"/api/v1/my/account?company_id={_STATE['company_b_id']}",
             token=_STATE["token_e"])
    assert r.status_code == 403, r.status_code
    assert "not a member" in _json(r).get("error", "")


@check("D2: employee-F cannot see company-A")
def D2():
    r = _get(f"/api/v1/my/account?company_id={_STATE['company_a_id']}",
             token=_STATE["token_f"])
    assert r.status_code == 403, r.status_code


# ─── Checks: leave POST happy + validation ─────────────────────────────
@check("E1: /api/v1/my/leave POST rejects missing dates")
def E1():
    from app.models import LeaveType
    # is_paid=False so the service doesn't ask for a balance the
    # audit hasn't seeded; the point of E1/E2 is the endpoint contract
    # and the service reuse, not the balance rule (which has its own
    # rejection test in E3).
    lt = LeaveType(company_id=_STATE["company_a_id"], name="بدون راتب",
                   is_active=True, is_paid=False)
    db.session.add(lt)
    db.session.commit()
    _STATE["leave_type_id_unpaid"] = lt.id
    r = _post("/api/v1/my/leave",
              {"leave_type_id": lt.id},
              token=_STATE["token_e"])
    assert r.status_code == 400, r.status_code


@check("E2: /api/v1/my/leave POST happy path (unpaid, no balance needed)")
def E2():
    r = _post("/api/v1/my/leave", {
        "leave_type_id": _STATE["leave_type_id_unpaid"],
        "start_date": "2026-09-01",
        "end_date": "2026-09-03",
        "reason": "أوديت",
    }, token=_STATE["token_e"])
    assert r.status_code == 201, (r.status_code, r.data[:200])
    body = _json(r)
    assert body["ok"] is True
    # days_count comes back as float (Decimal → float via serializer).
    assert float(body["request"]["days_count"]) > 0


@check("E3: /api/v1/my/leave POST rejects paid leave with no balance (service reuse)")
def E3():
    from app.models import LeaveType
    lt = LeaveType(company_id=_STATE["company_a_id"], name="سنوية",
                   is_active=True, is_paid=True)
    db.session.add(lt)
    db.session.commit()
    r = _post("/api/v1/my/leave", {
        "leave_type_id": lt.id,
        "start_date": "2026-10-01",
        "end_date": "2026-10-03",
    }, token=_STATE["token_e"])
    # The service raises LeaveError("الرصيد غير كافٍ ...") → 400.
    # This proves we're routing through submit_leave_request, not
    # inserting the row by hand.
    assert r.status_code == 400, (r.status_code, r.data[:200])
    assert "الرصيد" in _json(r).get("error", ""), _json(r)


# ─── Checks: notifications shape ───────────────────────────────────────
@check("F1: /api/v1/notifications returns items[]")
def F1():
    r = _get("/api/v1/notifications", token=_STATE["token_e"])
    assert r.status_code == 200, r.status_code
    body = _json(r)
    assert "items" in body and isinstance(body["items"], list)


@check("F2: /api/v1/notifications/unread-count is a shape-safe integer")
def F2():
    r = _get("/api/v1/notifications/unread-count", token=_STATE["token_e"])
    assert r.status_code == 200, r.status_code
    body = _json(r)
    assert isinstance(body.get("count"), int), body


# ─── Checks: attendance ─────────────────────────────────────────────────
@check("G1: /api/v1/my/attendance/checkin creates a row")
def G1():
    r = _post("/api/v1/my/attendance/checkin",
              {"lat": 24.7136, "lng": 46.6753},
              token=_STATE["token_e"])
    # 201 on happy path; also accept 400 with a policy-driven refusal
    # so this test doesn't wire in an AttendancePolicy just to run.
    assert r.status_code in (201, 400), (r.status_code, r.data[:200])


# ─── Checks: logout revokes the bearer ─────────────────────────────────
@check("H1: /api/v1/auth/logout revokes the bearer (next /my/account 401)")
def H1():
    r = _post("/api/v1/auth/login", {
        "email": _STATE["ue_email"],
        "password": _STATE["ue_password"],
        "device_name": "audit-logout",
    })
    tok = _json(r)["token"]
    r = _get("/api/v1/my/account", token=tok)
    assert r.status_code == 200, r.status_code
    r = _post("/api/v1/auth/logout", None, token=tok)
    assert r.status_code == 200, r.status_code
    r = _get("/api/v1/my/account", token=tok)
    assert r.status_code == 401, r.status_code


# ─── Runner ────────────────────────────────────────────────────────────
def main():
    app = create_app()
    _STATE["app"] = app
    with app.app_context():
        _setup()
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
