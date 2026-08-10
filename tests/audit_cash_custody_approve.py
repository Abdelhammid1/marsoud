#!/usr/bin/env python3
"""MARSOUD-CUSTODY-REQUEST-APPROVE-01 (2026-08-10) — audit for
the amount-override + receipt-attach path on cash-custody
request approval.

Locks:
- CASH_CUSTODY_REQUEST source_type accepted by the enum + the
  save_document whitelist + the _can_attach_to auth branch.
- approve_custody_request(amount=X) overrides the amount on
  the resulting CashCustody row; req.amount stays untouched
  (audit trail preserved).
- Approve without an amount override behaves exactly as
  before (regression on the happy path in
  audit_cash_custody.py:206-224).
- amount=0 / amount<0 → CustodyError.
- POST /custody/requests/<id>/approve with a receipt file
  writes both the custody row AND the Document row in one
  submit; a bad file doesn't roll back the approval.
- The accountant's requests page renders the approved-vs-
  requested cell + receipt link (browser-render smoke per
  [[browser-render-touched-pages]]).

Every check verified to fail against pre-ticket HEAD (the
approve service refuses the `amount` kwarg with a TypeError;
save_document rejects CASH_CUSTODY_REQUEST at the whitelist).
"""
import io
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

_ORIG_CREATE_APP = create_app
def create_app(*a, **kw):
    app = _ORIG_CREATE_APP(*a, **kw)
    app.config["SESSION_COOKIE_DOMAIN"] = None
    return app


CHECKS = []
PREFIX = "__CUSTAPP_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _fake_file(name="bank_slip.png"):
    from werkzeug.datastructures import FileStorage
    payload = b"\x89PNG\r\n\x1a\n" + b"AUDIT_TEST_STUB"
    return FileStorage(
        stream=io.BytesIO(payload),
        filename=name,
        content_type="image/png",
    )


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, UserStatus,
        Employee, CashCustodyRequest, CustodyRequestStatus,
        CustodyHolderType, Account,
    )
    from app.models.user import user_companies
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.roles import set_membership_role
    from app.services.legal import get_terms_version
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code=f"{PREFIX}plan").first()
    if not plan:
        plan = Plan(code=f"{PREFIX}plan", name="CUSTAPP",
                    name_ar="CUSTAPP", allowed_subitems=None)
        plan.set_modules(["accounting", "hr", "cash_custody",
                           "settings"])
        db.session.add(plan); db.session.flush()

    co = Company(name=f"{PREFIX}CO", base_currency="SAR",
                  plan_id=plan.id,
                  subscription_started_at=datetime.utcnow(),
                  subscription_expires_at=datetime.utcnow()
                    + timedelta(days=365))
    db.session.add(co); db.session.flush()
    db.session.commit()
    ensure_roles_ready_for_company(co.id)

    def _mk_user(email, name):
        u = User(email=email, full_name=name, is_active=True,
                  status=UserStatus.ACTIVE.value,
                  email_verified_at=datetime.utcnow(),
                  terms_version=get_terms_version(),
                  terms_accepted_at=datetime.utcnow(),
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"))
        db.session.add(u); db.session.flush()
        return u

    owner = _mk_user(f"{PREFIX}owner@x.test", "custapp owner")
    accountant = _mk_user(f"{PREFIX}acc@x.test", "custapp acc")
    holder = _mk_user(f"{PREFIX}hold@x.test", "custapp holder")
    stranger = _mk_user(f"{PREFIX}str@x.test", "custapp stranger")

    for u, role in ((owner, "owner"), (accountant, "accountant"),
                     (holder, "team_member"),
                     (stranger, "team_member")):
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=co.id, role=role))
    db.session.commit()
    for u, role in ((owner, "owner"), (accountant, "accountant"),
                     (holder, "team_member"),
                     (stranger, "team_member")):
        set_membership_role(u.id, co.id, role)

    emp = Employee(company_id=co.id, name="Holder Employee",
                    user_id=holder.id)
    db.session.add(emp); db.session.flush()

    # issue_custody posts the disbursement journal and needs the
    # standard chart (1110 cash, 1180 custody parent, etc.). The
    # seed_default_coa service knows how to populate everything.
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(co.id)

    # The request that every check operates against. Amount 500 —
    # override tests will approve 300 or 700 to prove the ledger
    # honours the override.
    req = CashCustodyRequest(
        company_id=co.id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
        amount=Decimal("500.00"),
        purpose="audit test purpose",
        status=CustodyRequestStatus.PENDING,
    )
    db.session.add(req); db.session.flush()
    db.session.commit()

    _STATE.update(
        co_id=co.id, owner_id=owner.id, acc_id=accountant.id,
        holder_id=holder.id, stranger_id=stranger.id,
        emp_id=emp.id, req_id=req.id,
    )


def _teardown():
    from app.models import Company, User, Plan, Document
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        Document.query.filter_by(company_id=cid).delete(
            synchronize_session=False)
        from flask import current_app
        try:
            doc_dir = Path(current_app.root_path) / "static" / "docs" / str(cid)
            if doc_dir.exists():
                shutil.rmtree(doc_dir, ignore_errors=True)
        except Exception:
            pass
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id=:c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id=:c"), {"c": cid})
        db.session.commit()
    for p in Plan.query.filter(Plan.code.like(f"{PREFIX}%")).all():
        db.session.delete(p)
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _reset_g():
    from flask import g
    for key in ("_login_user", "active_company", "user_companies",
                "impersonating"):
        try:
            g.pop(key, None)
        except Exception:
            pass


def _client_as(user_id):
    from flask import current_app
    _reset_g()
    db.session.expire_all()
    db.session.remove()
    c = current_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["co_id"]
    return c


def _fresh_request():
    """Some checks consume the seed request (approve mutates
    status). This resets it back to PENDING with a fresh row
    when the previous check already approved/rejected it."""
    from app.models import (
        CashCustodyRequest, CustodyRequestStatus, CustodyHolderType,
        CashCustody,
    )
    db.session.expire_all()
    req = db.session.get(CashCustodyRequest, _STATE["req_id"])
    if req and req.status == CustodyRequestStatus.PENDING:
        return req
    # Delete the linked custody (if any) + reset the row.
    CashCustody.query.filter_by(request_id=_STATE["req_id"]).delete(
        synchronize_session=False)
    db.session.commit()
    if req:
        req.status = CustodyRequestStatus.PENDING
        req.reviewed_by = None
        req.reviewed_at = None
        req.review_note = None
        db.session.commit()
    return req


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. Schema: DocumentSourceType.CASH_CUSTODY_REQUEST + whitelist")
def _():
    from app.models import DocumentSourceType
    from app.services.opsflow_extras import (
        save_document, DocumentError,
    )
    values = {v.value for v in DocumentSourceType}
    assert "CASH_CUSTODY_REQUEST" in values, (
        "CASH_CUSTODY_REQUEST missing from enum")
    # Prove the whitelist accepts it by round-tripping a save.
    doc = save_document(
        company_id=_STATE["co_id"],
        source_type="CASH_CUSTODY_REQUEST",
        source_id=_STATE["req_id"],
        file_storage=_fake_file("schema_probe.png"),
        uploaded_by_id=_STATE["owner_id"],
    )
    assert doc is not None
    assert "/cash_custody_request/" in doc.file_path, (
        f"unexpected directory: {doc.file_path}")
    return f"enum + whitelist both accept it → {doc.file_path}"


@check("2. approve_custody_request(amount=…) overrides; req.amount stays")
def _():
    from app.services.cash_custody import approve_custody_request
    from app.models import CashCustodyRequest
    req = _fresh_request()
    original = req.amount
    custody = approve_custody_request(
        req, reviewer_id=_STATE["owner_id"],
        amount=Decimal("300.00"),
        review_note="partial disbursement",
    )
    assert custody.amount_issued == Decimal("300.00"), (
        f"override didn't stick: {custody.amount_issued}")
    db.session.expire_all()
    r_after = db.session.get(CashCustodyRequest, _STATE["req_id"])
    assert r_after.amount == original, (
        f"req.amount was mutated: {r_after.amount}")
    return (f"custody.amount_issued=300; "
            f"req.amount unchanged at {r_after.amount}")


@check("3. approve_custody_request() with no amount → old behaviour")
def _():
    """Regression on audit_cash_custody.py:206-224 shape —
    calling without the amount kwarg must issue for exactly
    req.amount."""
    from app.services.cash_custody import approve_custody_request
    req = _fresh_request()
    custody = approve_custody_request(
        req, reviewer_id=_STATE["owner_id"])
    assert custody.amount_issued == req.amount, (
        f"legacy path drifted: {custody.amount_issued} vs {req.amount}")
    return f"legacy call → custody.amount_issued=req.amount ({req.amount})"


@check("4. approve_custody_request(amount=0) refused")
def _():
    from app.services.cash_custody import (
        approve_custody_request, CustodyError,
    )
    req = _fresh_request()
    try:
        approve_custody_request(
            req, reviewer_id=_STATE["owner_id"],
            amount=Decimal("0"))
    except CustodyError as e:
        assert "أكبر من صفر" in str(e), f"wrong msg: {e}"
        return f"amount=0 refused: {e}"
    raise AssertionError("amount=0 accepted — validation missing")


@check("5. approve_custody_request(amount=-10) refused")
def _():
    from app.services.cash_custody import (
        approve_custody_request, CustodyError,
    )
    req = _fresh_request()
    try:
        approve_custody_request(
            req, reviewer_id=_STATE["owner_id"],
            amount=Decimal("-10"))
    except CustodyError as e:
        assert "أكبر من صفر" in str(e), f"wrong msg: {e}"
        return f"negative refused: {e}"
    raise AssertionError("negative amount accepted")


@check("6. _can_attach_to: owner + accountant + holder allowed; stranger not")
def _():
    from flask import g, current_app
    from flask_login import login_user, logout_user
    from app.models import User, Company
    from app.routes.opsflow_extras import _can_attach_to

    def _login_and_check(user_id, expected):
        with current_app.test_request_context():
            _reset_g()
            db.session.expire_all()
            g.active_company = db.session.get(Company, _STATE["co_id"])
            login_user(db.session.get(User, user_id))
            got = _can_attach_to("CASH_CUSTODY_REQUEST",
                                   _STATE["req_id"], _STATE["co_id"])
            logout_user()
            assert got is expected, (
                f"user {user_id}: want {expected}, got {got}")

    _login_and_check(_STATE["owner_id"], True)
    _login_and_check(_STATE["acc_id"], True)
    _login_and_check(_STATE["holder_id"], True)
    _login_and_check(_STATE["stranger_id"], False)
    return "owner + accountant + holder allowed; stranger refused"


@check("7. POST /custody/requests/<id>/approve with amount + receipt")
def _():
    from app.models import (
        CashCustodyRequest, CashCustody, Document,
        CustodyRequestStatus,
    )
    req = _fresh_request()
    c = _client_as(_STATE["owner_id"])
    r = c.post(
        f"/custody/requests/{_STATE['req_id']}/approve",
        data={
            "amount": "350.00",
            "issued_on": date.today().isoformat(),
            "review_note": "audit HTTP test",
            "receipt": (io.BytesIO(b"\x89PNG\r\n\x1a\nHTTP"),
                         "http_slip.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    db.session.expire_all()
    r_after = db.session.get(CashCustodyRequest, _STATE["req_id"])
    assert r_after.status == CustodyRequestStatus.APPROVED, (
        f"request status: {r_after.status}")
    custody = CashCustody.query.filter_by(
        request_id=_STATE["req_id"]).first()
    assert custody is not None
    assert custody.amount_issued == Decimal("350.00"), (
        f"HTTP override didn't stick: {custody.amount_issued}")
    docs = Document.query.filter_by(
        source_type="CASH_CUSTODY_REQUEST",
        source_id=_STATE["req_id"],
    ).all()
    names = [d.name for d in docs]
    assert "http_slip.png" in names, (
        f"receipt not saved: {names}")
    return (f"POST approved 350 + saved receipt "
            f"({len(docs)} doc row(s))")


@check("8. Bad file extension: approve succeeds, receipt refused")
def _():
    """The approve is a money-moving operation — a bad file
    must not roll it back. Flash a warning + keep the
    approval standing."""
    from app.models import (
        CashCustodyRequest, CashCustody, Document,
        CustodyRequestStatus,
    )
    req = _fresh_request()
    c = _client_as(_STATE["owner_id"])
    r = c.post(
        f"/custody/requests/{_STATE['req_id']}/approve",
        data={
            "amount": "500.00",
            "issued_on": date.today().isoformat(),
            "receipt": (io.BytesIO(b"payload"), "bad_ext.xyz"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    db.session.expire_all()
    r_after = db.session.get(CashCustodyRequest, _STATE["req_id"])
    assert r_after.status == CustodyRequestStatus.APPROVED, (
        "approve rolled back on bad file — should have stood")
    custody = CashCustody.query.filter_by(
        request_id=_STATE["req_id"]).first()
    assert custody is not None
    docs = Document.query.filter_by(
        source_type="CASH_CUSTODY_REQUEST",
        source_id=_STATE["req_id"],
    ).all()
    bad_names = [d.name for d in docs if d.name == "bad_ext.xyz"]
    assert not bad_names, (
        f"unsupported extension slipped through: {bad_names}")
    return "approve stood; bad file refused (no rollback)"


def _approve_with_override(amount, receipt_name):
    """Set up an override-approved state: reset the request to
    PENDING, approve for a DIFFERENT amount, attach a receipt.
    Used by the browser-render smoke checks below so they don't
    depend on the specific state left by check 7 / check 8."""
    from app.services.cash_custody import approve_custody_request
    from app.services.opsflow_extras import save_document
    req = _fresh_request()
    approve_custody_request(
        req, reviewer_id=_STATE["owner_id"],
        amount=Decimal(str(amount)),
        review_note="render smoke",
    )
    save_document(
        company_id=_STATE["co_id"],
        source_type="CASH_CUSTODY_REQUEST",
        source_id=_STATE["req_id"],
        file_storage=_fake_file(receipt_name),
        uploaded_by_id=_STATE["owner_id"],
    )


@check("9. GET /custody/requests body renders 'اعتُمد' cell + receipt")
def _():
    """Browser-render smoke — the amount cell must show the
    override + link to the file for a request that was
    approved with a DIFFERENT amount than requested."""
    _approve_with_override(350, "render_slip.png")
    c = _client_as(_STATE["owner_id"])
    r = c.get("/custody/requests?status=APPROVED")
    assert r.status_code == 200, f"HTTP {r.status_code}"
    body = r.get_data(as_text=True)
    assert "اعتُمد:" in body, (
        "approved-vs-requested label missing from requests page")
    assert "render_slip.png" in body, (
        "receipt filename not rendered in requests page")
    return "requests page shows override + receipt link"


@check("10. Portal /my/custody body shows override + receipt to employee")
def _():
    """The holder employee sees on their portal exactly what
    the accountant saw: requested + approved + receipt."""
    # State from check 9 carries over — request is approved
    # for 350 with render_slip.png attached.
    c = _client_as(_STATE["holder_id"])
    r = c.get("/my/custody")
    assert r.status_code == 200, f"HTTP {r.status_code}"
    body = r.get_data(as_text=True)
    assert "اعتُمد:" in body, (
        "employee portal missing the approved-vs-requested label")
    assert "render_slip.png" in body, (
        "employee portal missing the receipt link")
    return "portal shows employee both amounts + receipt"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(fixture cleaned up)")
    print(f"\n----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
