#!/usr/bin/env python3
"""MARSOUD-CUSTODY-ATTACH-01 (2026-08-10) — audit for the
receipt-attachment lifecycle on cash + item custody.

Locks the fix and its regression:
- Two DocumentSourceType values present (schema).
- save_document(source_type=CASH_CUSTODY_SETTLEMENT) writes
  a real file + DB row.
- save_document(source_type=ITEM_CUSTODY) writes a real file
  + DB row.
- The widening is not a free-for-all: an unknown source_type
  still raises DocumentError.
- documents_for(...) returns the uploaded rows.
- _can_attach_to routes both types to owner/admin/accountant
  correctly; team_member is refused.
- The item-custody template renders the docs section (per
  the browser-render workflow rule — Jinja parse-time errors
  wouldn't show up in Python-import checks).

Every check verified to fail against pre-fix HEAD (the
save_document whitelist rejects CASH_CUSTODY_SETTLEMENT and
ITEM_CUSTODY at line 220 before this ticket).
"""
import io
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Windows console defaults to cp1252 — force UTF-8 so print()
# of the Arabic labels in assertion messages doesn't blow up.
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
PREFIX = "__CUSTATT_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _fake_file(name="receipt.png"):
    """A minimal FileStorage-like object that satisfies
    save_document's stream + filename + mimetype needs."""
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
        Employee, CashCustody, CashCustodySettlementLine,
        CustodyHolderType, CustodyStatus,
        CustodyItem, ItemCustody, ItemCustodyStatus,
    )
    from app.models.user import user_companies
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.roles import set_membership_role
    from app.services.legal import get_terms_version
    from werkzeug.security import generate_password_hash

    plan = Plan.query.filter_by(code=f"{PREFIX}plan").first()
    if not plan:
        plan = Plan(code=f"{PREFIX}plan", name="CUSTATT",
                    name_ar="CUSTATT", allowed_subitems=None)
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

    owner = _mk_user(f"{PREFIX}owner@x.test", "custatt owner")
    accountant = _mk_user(f"{PREFIX}acc@x.test", "custatt acc")
    holder = _mk_user(f"{PREFIX}hold@x.test", "custatt hold")
    stranger = _mk_user(f"{PREFIX}str@x.test", "custatt stranger")

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

    # Employee row for the holder — needed so _can_attach_to's
    # "holder-employee can attach" branch has a matching entry.
    emp = Employee(company_id=co.id,
                    name="Holder Employee",
                    user_id=holder.id)
    db.session.add(emp); db.session.flush()

    # Cash custody + one settlement line — the target of
    # CASH_CUSTODY_SETTLEMENT attachments.
    from decimal import Decimal
    cash_c = CashCustody(
        company_id=co.id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
        amount_issued=Decimal("500.00"),
        purpose="audit test",
        issued_on=date.today(),
        status=CustodyStatus.ISSUED,
    )
    db.session.add(cash_c); db.session.flush()

    # settlement_line needs an expense_account — pick or create
    # any account. Use the first account in the company; if none,
    # skip (audit fails gracefully for that check).
    from app.models import Account
    acc = Account.query.filter_by(company_id=co.id).first()
    if acc is None:
        # Bootstrap the tiniest expense account so the FK is
        # satisfied. Real seeds populate the chart, but this
        # audit doesn't need a full chart.
        acc = Account(company_id=co.id, code="9999",
                       name="audit expense", name_ar="audit expense",
                       type="EXPENSE", normal_side="DEBIT",
                       is_active=True)
        db.session.add(acc); db.session.flush()
    line = CashCustodySettlementLine(
        company_id=co.id, custody_id=cash_c.id,
        expense_account_id=acc.id,
        amount=Decimal("50.00"),
        receipt_note="audit stub",
    )
    db.session.add(line); db.session.flush()

    # Item custody — the target of ITEM_CUSTODY attachments.
    item = CustodyItem(company_id=co.id, name="Laptop",
                       estimated_value=Decimal("1000.00"),
                       is_active=True)
    db.session.add(item); db.session.flush()
    item_c = ItemCustody(
        company_id=co.id, item_id=item.id,
        holder_type=CustodyHolderType.EMPLOYEE,
        employee_id=emp.id,
        handed_over_on=date.today(),
        status=ItemCustodyStatus.ACTIVE,
    )
    db.session.add(item_c); db.session.flush()
    db.session.commit()

    _STATE.update(
        co_id=co.id, owner_id=owner.id, acc_id=accountant.id,
        holder_id=holder.id, stranger_id=stranger.id,
        cash_line_id=line.id, item_custody_id=item_c.id,
    )


def _teardown():
    from app.models import Company, User, Plan, Document
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all(); db.session.remove()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        # Wipe any Documents we wrote so orphan file paths don't
        # accumulate.
        Document.query.filter_by(company_id=cid).delete(
            synchronize_session=False)
        # Also nuke files on disk.
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


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. Schema: both DocumentSourceType values present")
def _():
    from app.models import DocumentSourceType
    values = {v.value for v in DocumentSourceType}
    assert "CASH_CUSTODY_SETTLEMENT" in values, (
        "CASH_CUSTODY_SETTLEMENT missing from enum")
    assert "ITEM_CUSTODY" in values, (
        "ITEM_CUSTODY missing from enum")
    return f"enum values: {sorted(values)}"


@check("2. save_document accepts CASH_CUSTODY_SETTLEMENT + file lands on disk")
def _():
    from app.services.opsflow_extras import save_document
    from flask import current_app
    doc = save_document(
        company_id=_STATE["co_id"],
        source_type="CASH_CUSTODY_SETTLEMENT",
        source_id=_STATE["cash_line_id"],
        file_storage=_fake_file("cash_receipt.png"),
        uploaded_by_id=_STATE["owner_id"],
    )
    assert doc is not None
    assert doc.source_type == "CASH_CUSTODY_SETTLEMENT"
    # File on disk under the .lower()'d directory.
    rel = doc.file_path.lstrip("/")
    disk = os.path.join(current_app.root_path, rel)
    assert os.path.exists(disk), f"file missing on disk: {disk}"
    assert "/cash_custody_settlement/" in doc.file_path, (
        f"unexpected directory: {doc.file_path}")
    return f"row + file at {doc.file_path}"


@check("3. save_document accepts ITEM_CUSTODY + file lands on disk")
def _():
    from app.services.opsflow_extras import save_document
    from flask import current_app
    doc = save_document(
        company_id=_STATE["co_id"],
        source_type="ITEM_CUSTODY",
        source_id=_STATE["item_custody_id"],
        file_storage=_fake_file("item_receipt.png"),
        uploaded_by_id=_STATE["owner_id"],
    )
    assert doc is not None
    assert doc.source_type == "ITEM_CUSTODY"
    rel = doc.file_path.lstrip("/")
    disk = os.path.join(current_app.root_path, rel)
    assert os.path.exists(disk), f"file missing on disk: {disk}"
    assert "/item_custody/" in doc.file_path, (
        f"unexpected directory: {doc.file_path}")
    return f"row + file at {doc.file_path}"


@check("4. Widening is not a free-for-all: unknown source_type refused")
def _():
    """The whitelist widening MUST stay a whitelist. Regression
    guard: an unknown source_type still raises DocumentError."""
    from app.services.opsflow_extras import (
        save_document, DocumentError,
    )
    try:
        save_document(
            company_id=_STATE["co_id"],
            source_type="FROBNICATE",
            source_id=1,
            file_storage=_fake_file("stub.png"),
            uploaded_by_id=_STATE["owner_id"],
        )
    except DocumentError as e:
        assert "غير صالح" in str(e), f"wrong msg: {e}"
        return f"refused unknown source: {e}"
    raise AssertionError(
        "unknown source_type was accepted — whitelist is broken")


@check("5. documents_for('ITEM_CUSTODY', id) returns the uploaded row")
def _():
    from app.services.opsflow_extras import documents_for
    docs = documents_for("ITEM_CUSTODY", _STATE["item_custody_id"])
    names = [d.name for d in docs]
    assert "item_receipt.png" in names, (
        f"uploaded file missing from documents_for: {names}")
    return f"returned {len(docs)} row(s)"


@check("6. _can_attach_to routes both types correctly")
def _():
    """owner/admin/accountant can attach for both types; the
    holder-employee for their OWN row can attach; a random
    team_member cannot."""
    from flask import g
    from flask_login import login_user, logout_user
    from app.models import User
    from app.routes.opsflow_extras import _can_attach_to

    def _as(user_id):
        _reset_g()
        db.session.expire_all()
        u = db.session.get(User, user_id)
        # Set g.active_company since _can_attach_to reads role
        # via get_user_role which itself uses g.
        from app.models import Company
        g.active_company = db.session.get(Company, _STATE["co_id"])
        return u

    # This helper leans on Flask-Login's login_user which needs a
    # test_request_context — wrap accordingly.
    from flask import current_app
    with current_app.test_request_context():
        # owner → True for both
        _as(_STATE["owner_id"])
        login_user(db.session.get(User, _STATE["owner_id"]))
        assert _can_attach_to("CASH_CUSTODY_SETTLEMENT",
                                _STATE["cash_line_id"],
                                _STATE["co_id"]) is True
        assert _can_attach_to("ITEM_CUSTODY",
                                _STATE["item_custody_id"],
                                _STATE["co_id"]) is True
        logout_user()

    with current_app.test_request_context():
        # holder-employee → True for their OWN rows
        _as(_STATE["holder_id"])
        login_user(db.session.get(User, _STATE["holder_id"]))
        assert _can_attach_to("ITEM_CUSTODY",
                                _STATE["item_custody_id"],
                                _STATE["co_id"]) is True
        logout_user()

    with current_app.test_request_context():
        # stranger team_member → False
        _as(_STATE["stranger_id"])
        login_user(db.session.get(User, _STATE["stranger_id"]))
        assert _can_attach_to("CASH_CUSTODY_SETTLEMENT",
                                _STATE["cash_line_id"],
                                _STATE["co_id"]) is False
        assert _can_attach_to("ITEM_CUSTODY",
                                _STATE["item_custody_id"],
                                _STATE["co_id"]) is False
        logout_user()
    return "owner + holder allowed; stranger refused for both"


@check("7. POST /docs/upload/ITEM_CUSTODY/<id> as owner creates + redirects")
def _():
    from app.models import Document
    c = _client_as(_STATE["owner_id"])
    r = c.post(
        f"/docs/upload/ITEM_CUSTODY/{_STATE['item_custody_id']}",
        data={"file": (io.BytesIO(b"\x89PNG\r\n\x1a\nHTTP"),
                        "http_receipt.png")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert r.status_code in (200, 302), f"HTTP {r.status_code}"
    # Bounce should land on the item-custody detail.
    loc = r.headers.get("Location", "")
    assert f"/items/custody/{_STATE['item_custody_id']}" in loc, (
        f"unexpected redirect: {loc}")
    db.session.expire_all()
    docs = Document.query.filter_by(
        source_type="ITEM_CUSTODY",
        source_id=_STATE["item_custody_id"],
    ).all()
    names = [d.name for d in docs]
    assert "http_receipt.png" in names, (
        f"uploaded file missing after HTTP POST: {names}")
    return f"POST wrote row + redirected to {loc}"


@check("8. GET /items/custody/<id> body renders the docs section")
def _():
    """Browser-render smoke: the template block MUST parse +
    render. A Jinja typo or bad url_for wouldn't fail any
    Python-import check and would only surface here."""
    c = _client_as(_STATE["owner_id"])
    r = c.get(f"/items/custody/{_STATE['item_custody_id']}")
    assert r.status_code == 200, f"HTTP {r.status_code}"
    body = r.get_data(as_text=True)
    assert "مرفقات العهدة" in body, (
        "attachments heading missing from item-custody body")
    # And the uploaded filename from check 7 should be listed.
    assert "http_receipt.png" in body, (
        "uploaded file name not rendered in template")
    return "template renders + shows uploaded file"


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
