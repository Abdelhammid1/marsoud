#!/usr/bin/env python3
"""MARSOUD-FINANCIAL-ACCOUNT-FIELD (2026-08-04).

The 🧮 accounting-operations wizards asked for a PAYMENT METHOD, which was
the wrong question twice over:

  a) "نقدي" appeared twice. The template hardcoded a blank
     `<option>نقدي (الصندوق)</option>` that resolved to code 1110, and the
     seeded "نقدي" payment method points at 1110 as well. Two options,
     identical effect.
  b) One bank, ever. The seeded "bank" method points at 1124/CIB (header
     1120 refuses journal lines), so money put into بنك مصر was posted to
     CIB — a real distortion of the bank balances, not a UI complaint.

The field now offers the financial ACCOUNTS themselves, grouped الصندوق /
البنوك, postable only.

Checks:
  1. The helper groups cash + every bank, with no duplicate.
  2. Header 1120 is never offered.
  3. A bank the company added by hand appears too.
  4. A deactivated account drops out.
  5. The rendered form shows the groups and no hardcoded نقدي option.
  6. THE BUG: choosing بنك مصر posts to 1122, not to CIB/1124.
  7. Every operation uses the shared field — a future wizard inherits it.
  8. The field is required; a blank submission is refused, not defaulted.
  9. A crafted POST naming an out-of-set account is rejected.
 10. A cross-tenant account id is rejected.
"""
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
COMPANY_NAME = "__FIN_ACCT_FIELD__"
OTHER_NAME = "__FIN_ACCT_OTHER__"
EMAIL = "__finacct@audit.local"
_STATE = {}

# The default chart of accounts: cash + five real banks under header 1120.
BANK_CODES = ("1121", "1122", "1123", "1124", "1125")


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _mk_company(name):
    from app.models import Company, Plan
    from app.services.seed_coa import seed_default_coa
    from app.services.plan_gating import plan_allows
    co = Company(name=name, base_currency="EGP", vat_rate=0)
    db.session.add(co)
    db.session.flush()
    for pl in Plan.query.order_by(Plan.id).all():
        co.plan_id = pl.id
        co.intended_plan_id = pl.id
        db.session.flush()
        if plan_allows("journals.create", co):
            break
    db.session.commit()
    seed_default_coa(co.id)
    return co


def _setup():
    from app.models import User
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.roles import set_membership_role
    from app.services.legal import get_terms_version

    _teardown()
    co = _mk_company(COMPANY_NAME)
    other = _mk_company(OTHER_NAME)
    ensure_roles_ready_for_company(co.id)

    u = User(email=EMAIL, full_name="FinAcct Owner", is_active=True,
             terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u)
    db.session.flush()
    db.session.commit()
    set_membership_role(u.id, co.id, "owner")

    _STATE["cid"] = co.id
    _STATE["other_cid"] = other.id
    _STATE["uid"] = u.id


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text
    for name in (COMPANY_NAME, OTHER_NAME):
        co = Company.query.filter_by(name=name).first()
        if not co:
            continue
        cid = co.id
        db.session.execute(text(
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id=:c)"),
            {"c": cid})
        for tbl in ("journal_entries", "payment_methods", "accounts",
                    "user_companies"):
            try:
                db.session.execute(
                    text(f"DELETE FROM {tbl} WHERE company_id=:c"), {"c": cid})
            except Exception:
                db.session.rollback()
        try:
            db.session.execute(text(
                "DELETE FROM role_permissions WHERE role_id IN "
                "(SELECT id FROM roles WHERE company_id=:c)"), {"c": cid})
            db.session.execute(text("DELETE FROM roles WHERE company_id=:c"),
                               {"c": cid})
        except Exception:
            db.session.rollback()
        db.session.execute(text("DELETE FROM companies WHERE id=:c"),
                           {"c": cid})
        db.session.commit()
    u = User.query.filter_by(email=EMAIL).first()
    if u:
        db.session.delete(u)
        db.session.commit()


def _client():
    c = _STATE["app"].test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(_STATE["uid"])
        s["_fresh"] = True
        s["active_company_id"] = _STATE["cid"]
    return c


def _acc(code, cid=None):
    from app.services.ledger import get_account_by_code
    return get_account_by_code(cid or _STATE["cid"], code)


def _groups():
    from app.services.ledger import cash_and_bank_accounts
    return cash_and_bank_accounts(_STATE["cid"])


# ─── The list itself ────────────────────────────────────────────────────
@check("1. the field lists الصندوق + every bank, with no duplicate")
def _():
    groups = _groups()
    labels = [g[0] for g in groups]
    assert labels == ["الصندوق", "البنوك"], f"groups: {labels}"
    cash, banks = groups[0][1], groups[1][1]
    assert [a.code for a in cash] == ["1110"], \
        f"cash group: {[a.code for a in cash]}"
    assert [a.code for a in banks] == list(BANK_CODES), \
        f"bank group: {[a.code for a in banks]}"
    # No account may appear twice across the whole control.
    ids = [a.id for _l, accs in groups for a in accs]
    assert len(ids) == len(set(ids)), "an account is offered twice"
    return f"1 cash + {len(banks)} banks, {len(ids)} unique options"


@check("2. header 1120 (البنوك) is never offered as an option")
def _():
    header = _acc("1120")
    assert header is not None and not header.is_postable, \
        "fixture wrong: 1120 should be a non-postable header"
    ids = {a.id for _l, accs in _groups() for a in accs}
    assert header.id not in ids, \
        "1120 is offered — the form would fail only after submission"
    # And post_journal would indeed have refused it, which is the point.
    from app.services.ledger import post_journal, LedgerError
    try:
        post_journal(
            company_id=_STATE["cid"], description="audit — header probe",
            lines=[{"account_id": header.id, "debit": 1, "credit": 0},
                   {"account_id": _acc("3100").id, "debit": 0, "credit": 1}],
            entry_date=date.today(), source_type="audit_seed")
        raise AssertionError("post_journal accepted a header account")
    except LedgerError:
        pass
    return "1120 absent from the list, and still refused by post_journal"


@check("3. a bank added by hand shows up too")
def _():
    """The list must not be a hardcoded set of codes: a company that adds
    its own bank under 1120 has to see it."""
    from app.models import Account, AccountType, NormalSide
    parent = _acc("1120")
    extra = Account(company_id=_STATE["cid"], code="1129",
                    name="Custom Bank", name_ar="بنك مخصص",
                    type=AccountType.ASSET, normal_side=NormalSide.DEBIT,
                    parent_id=parent.id, is_active=True, is_postable=True)
    db.session.add(extra)
    db.session.commit()
    try:
        banks = dict(_groups())["البنوك"]
        assert extra.id in {a.id for a in banks}, \
            "a manually added bank is missing from the list"
        assert [a.code for a in banks] == list(BANK_CODES) + ["1129"], \
            f"ordering broke: {[a.code for a in banks]}"
    finally:
        db.session.delete(extra)
        db.session.commit()
    return "custom bank 1129 listed, in code order"


@check("4. a deactivated account drops out of the list")
def _():
    bank = _acc("1123")
    bank.is_active = False
    db.session.commit()
    try:
        ids = {a.id for _l, accs in _groups() for a in accs}
        assert bank.id not in ids, "an inactive account is still offered"
    finally:
        bank.is_active = True
        db.session.commit()
    return "inactive 1123 hidden"


@check("5. the rendered form shows the groups, with no hardcoded نقدي option")
def _():
    from app.services.accounting_ops import OPERATIONS
    for op in OPERATIONS:
        body = _client().get(f"/accounting-ops/{op.key}").get_data(as_text=True)
        assert '<optgroup label="الصندوق"' in body, f"{op.key}: no cash group"
        assert '<optgroup label="البنوك"' in body, f"{op.key}: no banks group"
        for code in BANK_CODES:
            name = _acc(code).name_ar
            assert name in body, f"{op.key}: bank {code} ({name}) missing"
        assert "نقدي (الصندوق)" not in body, \
            f"{op.key}: the duplicate hardcoded نقدي option is still there"
    tpl = (ROOT / "app/templates/accounting_ops/run.html").read_text(
        encoding="utf-8")
    assert "payment_methods" not in tpl, \
        "the template still loops payment methods"
    return f"{len(OPERATIONS)} wizards show 2 groups, 6 accounts"


# ─── THE BUG ────────────────────────────────────────────────────────────
@check("6. choosing بنك مصر posts to 1122 — not to CIB/1124")
def _():
    from app.models import JournalEntry
    from app.services.accounting_ops import get_operation, run_operation
    masr = _acc("1122")
    cib = _acc("1124")
    entry = run_operation(get_operation("capital"), _STATE["cid"], {
        "amount": "750", "date": date.today().isoformat(),
        "account_id": str(masr.id), "notes": "audit",
    }, actor_id=_STATE["uid"])
    e = db.session.get(JournalEntry, entry.id)
    debits = [l for l in e.lines if float(l.debit) > 0]
    assert len(debits) == 1, f"expected one debit line, got {len(debits)}"
    assert debits[0].account_id == masr.id, (
        "capital landed on the wrong bank — this is the reported bug")
    assert debits[0].account_id != cib.id
    # And the drawings wizard credits the chosen bank, not cash.
    e2 = db.session.get(JournalEntry, run_operation(
        get_operation("owner-drawings"), _STATE["cid"], {
            "amount": "100", "date": date.today().isoformat(),
            "account_id": str(masr.id),
        }, actor_id=_STATE["uid"]).id)
    credits = [l for l in e2.lines if float(l.credit) > 0]
    assert credits[0].account_id == masr.id, "drawings ignored the choice"
    return "capital Dr 1122 · drawings Cr 1122"


@check("7. every operation uses the shared field (future wizards inherit it)")
def _():
    from app.services.accounting_ops import OPERATIONS
    money_fields = 0
    for op in OPERATIONS:
        fields = [f for f in op.fields if f.kind == "financial_account"]
        assert len(fields) == 1, \
            f"{op.key}: expected exactly one financial_account field"
        f = fields[0]
        assert f.name == "account_id", \
            f"{op.key}: field name is {f.name!r}, the builders read account_id"
        assert f.required, f"{op.key}: the account field must be required"
        money_fields += 1
        assert not [x for x in op.fields if x.kind == "payment_method"], \
            f"{op.key}: still declares a payment_method field"
    return f"{money_fields}/{len(OPERATIONS)} wizards on the shared field"


# ─── Validation ─────────────────────────────────────────────────────────
@check("8. a blank account is refused, not silently defaulted to 1110")
def _():
    from app.services.accounting_ops import (
        get_operation, run_operation, OperationError,
    )
    for blank in ("", None, "0"):
        try:
            run_operation(get_operation("capital"), _STATE["cid"], {
                "amount": "10", "date": date.today().isoformat(),
                "account_id": blank,
            }, actor_id=_STATE["uid"])
            raise AssertionError(
                f"blank account {blank!r} was accepted — it used to fall "
                "back to 1110, which is half the duplicate-option bug")
        except OperationError as e:
            assert "اختر" in str(e) or "غير صالح" in str(e), str(e)
    return "blank rejected with a clear message"


@check("9. a crafted POST naming an out-of-set account is rejected")
def _():
    from app.services.accounting_ops import (
        get_operation, run_operation, OperationError,
    )
    # Revenue: a perfectly real account of this company, but not one the
    # form offers. The field must re-check against the allowed SET.
    for code in ("4100", "1120", "3100"):
        target = _acc(code)
        if not target:
            continue
        try:
            run_operation(get_operation("capital"), _STATE["cid"], {
                "amount": "10", "date": date.today().isoformat(),
                "account_id": str(target.id),
            }, actor_id=_STATE["uid"])
            raise AssertionError(
                f"{code} was accepted as a financial account")
        except OperationError as e:
            assert "غير صالح" in str(e), f"{code}: {e}"
    return "revenue / header / equity all refused"


@check("10. an account from another company is rejected")
def _():
    from app.services.accounting_ops import (
        get_operation, run_operation, OperationError,
    )
    foreign = _acc("1122", cid=_STATE["other_cid"])
    assert foreign is not None
    try:
        run_operation(get_operation("capital"), _STATE["cid"], {
            "amount": "10", "date": date.today().isoformat(),
            "account_id": str(foreign.id),
        }, actor_id=_STATE["uid"])
        raise AssertionError("a cross-tenant account was accepted")
    except OperationError as e:
        assert "غير صالح" in str(e), str(e)
    return "cross-tenant account refused"


def main():
    app = create_app()
    _STATE["app"] = app
    with app.app_context():
        _setup()
    passed = failed = 0
    try:
        for label, fn in CHECKS:
            try:
                with app.app_context():
                    result = fn()
                print(f"PASS  {label}\n        ⇒ {result}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
                failed += 1
    finally:
        with app.app_context():
            _teardown()
        print("\n(cleaned up fixture companies)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
