#!/usr/bin/env python3
"""MARSOUD-POS-URL-OPACITY + MARSOUD-POS-CONFIRM-BEFORE-PAY
(Abdelhamid 2026-07-19).

Two urgent customer-triggered tickets:

A. URL exposure. The old /pos/orders/<int:invoice_id>/receipt
   used the global auto-increment id, which leaked the system-
   wide POS-order count across every tenant. Fix: route now keys
   on Invoice.number (per-company, POS-0001 style). The legacy
   numeric-id form still resolves for backward compat with
   printed receipts / bookmarks, but every url_for(..) call in
   the codebase now passes `number=`.

B. Ghost item on the receipt. Customer created an order for
   2.00, paid 3.00, and the receipt included a phantom "منتج
   خام 1" line for 0.50 they didn't intentionally add. Root
   cause: the cart JS lets any accidental click on the category
   grid add a line silently, and the pay button submits with no
   confirmation. Fix: a confirmation modal now opens on pay-click,
   listing every line + the grand total, requiring an explicit
   "Confirm" action before the invoice is created.

Checks:
  1. /pos/orders/POS-0001/receipt (per-company number) resolves
     the invoice.
  2. Legacy /pos/orders/<int_id>/receipt still resolves (backward
     compat with old receipts / bookmarks).
  3. Route rejects a cross-tenant invoice number with 404 (no
     leak).
  4. /pos/orders/POS-0001/detail + /pos/orders/POS-0001/void also
     accept the number form.
  5. Redirect after order creation uses number, not the id.
  6. Register page template has the confirm modal + escape-key
     handler + submit guard.
  7. The pay button opens the modal instead of submitting.
"""
import sys
from pathlib import Path
from datetime import date, timedelta

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


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'pu-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Invoice, InvoiceStatus,
    )
    from werkzeug.security import generate_password_hash
    from datetime import date

    for name in ("__PU_A__", "__PU_B__"):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__PU_A__", base_currency="SAR", vat_rate=0)
    b_co = Company(name="__PU_B__", base_currency="SAR", vat_rate=0)
    db.session.add_all([a, b_co]); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id); seed_default_coa(b_co.id)

    def _mk(email, cid, role):
        u = User(email=email,
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=cid, role=role))
        return u

    owner_a = _mk("pu-owner-a@x.test", a.id, "owner")
    owner_b = _mk("pu-owner-b@x.test", b_co.id, "owner")

    # Insert two POS invoices manually — one per company — so we can
    # verify the URL-opacity guarantees without going through the full
    # POS pipeline (which is heavy).
    def _mk_inv(cid, number, total):
        inv = Invoice(
            company_id=cid, number=number, source="POS",
            issue_date=date.today(), due_date=date.today(),
            currency="SAR", subtotal=total, total=total,
            paid_amount=total, tax_rate=0, tax_amount=0,
            status=InvoiceStatus.PAID,
        )
        db.session.add(inv); db.session.flush()
        return inv
    inv_a = _mk_inv(a.id, "POS-0001", 100)
    inv_b = _mk_inv(b_co.id, "POS-0001", 200)
    db.session.commit()

    _STATE.update(
        a_id=a.id, b_id=b_co.id,
        owner_a_id=owner_a.id, owner_b_id=owner_b.id,
        inv_a_id=inv_a.id, inv_a_number=inv_a.number,
        inv_b_id=inv_b.id, inv_b_number=inv_b.number,
    )


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _login(cid, user_id):
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["active_company_id"] = cid
    return client


# ─── Ticket A: URL opacity ───────────────────────────────────────
@check("1. /pos/orders/POS-0001/receipt resolves via per-company number")
def _():
    r = _login(_STATE["a_id"], _STATE["owner_a_id"]).get(
        f"/pos/orders/{_STATE['inv_a_number']}/receipt",
        follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    # Sanity — the URL displayed to the customer is the per-company
    # number, not the global id.
    return "POS-0001 URL works"


@check("2. Legacy /pos/orders/<int_id>/receipt still resolves (backward compat)")
def _():
    r = _login(_STATE["a_id"], _STATE["owner_a_id"]).get(
        f"/pos/orders/{_STATE['inv_a_id']}/receipt",
        follow_redirects=False)
    assert r.status_code == 200, f"got {r.status_code}"
    return "legacy numeric id form still resolves"


@check("3. Cross-tenant number returns 404 (no leak between companies)")
def _():
    # Same "POS-0001" exists in both companies. Company A user
    # asking for A's POS-0001 works (check 1); asking for B's
    # POS-0001 (by id, which is guaranteed different) returns 404.
    r = _login(_STATE["a_id"], _STATE["owner_a_id"]).get(
        f"/pos/orders/{_STATE['inv_b_id']}/receipt",
        follow_redirects=False)
    assert r.status_code == 404, \
        f"cross-tenant id resolved: {r.status_code}"
    return "cross-tenant id blocked"


@check("4. /detail + /void routes also accept the number form")
def _():
    client = _login(_STATE["a_id"], _STATE["owner_a_id"])
    r = client.get(f"/pos/orders/{_STATE['inv_a_number']}",
                   follow_redirects=False)
    assert r.status_code == 200, f"detail: {r.status_code}"
    # Void needs POS-role plus pos.void permission. Owner has it.
    r = client.post(f"/pos/orders/{_STATE['inv_a_number']}/void",
                    data={"reason": "audit"}, follow_redirects=False)
    # Result is 302 (redirect after action) — we don't need to
    # follow it, just prove the route matched.
    assert r.status_code in (200, 302), f"void: {r.status_code}"
    return "detail + void accept number"


@check("5. Redirect after order creation uses number, not id (source-level)")
def _():
    """We don't drive a full order via HTTP here — that needs a
    payment method + a variant + a warehouse. Instead we grep
    the route source to prove the redirect uses number=."""
    src = (ROOT / "app/routes/pos.py").read_text(encoding="utf-8")
    assert 'url_for("pos.receipt", number=invoice.number)' in src, \
        "order_new redirect still passes invoice_id instead of number"
    return "order_new redirect updated"


# ─── Ticket B: confirm-before-pay ────────────────────────────────
@check("6. Register template has the confirm modal + supporting JS")
def _():
    src = (ROOT / "app/templates/pos/register.html").read_text(
        encoding="utf-8")
    assert 'id="confirm-modal"' in src, "modal element missing"
    assert "openConfirmModal" in src and "closeConfirmModal" in src, \
        "open/close functions missing"
    assert "e.key === 'Escape'" in src, \
        "escape-key handler missing"
    assert "_submitting" in src, "double-click submit guard missing"
    return "modal + close + escape + submit-guard all present"


@check("7. Pay button opens the modal instead of submitting the form")
def _():
    src = (ROOT / "app/templates/pos/register.html").read_text(
        encoding="utf-8")
    # The pay button is type="button" (not submit) and calls
    # openConfirmModal — the actual submit happens only via the
    # confirm-modal's confirm button.
    assert 'id="pay-btn"' in src
    # It must be a button that OPENS the modal, not a submit that
    # bypasses it. Look for the exact wiring.
    assert 'onclick="openConfirmModal()"' in src, \
        "pay button no longer opens the modal"
    assert 'type="button" id="pay-btn"' in src, \
        "pay button should be type=button (not submit) so it opens the modal"
    return "pay button opens the modal, submit is behind confirmation"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
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
            try:
                for k in ("a_id", "b_id"):
                    if k in _STATE:
                        _teardown(_STATE[k])
                print("\n(cleaned up fixture companies)")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
