#!/usr/bin/env python3
"""MARSOUD-OPSFLOW-AUDIT-FLUSH-01 — the opsflow AuditEntry listeners
must insert via the flush's connection, not via `db.session.add()`,
because `after_insert` / `after_update` / `after_delete` fire during
a flush and SQLAlchemy forbids re-entering the Session from there.

Before this ticket, `app/services/opsflow_extras.py:162` called
`db.session.add(entry)` inside the listener, which triggered a
recurring `SAWarning: Usage of the 'Session.add()' operation is not
currently supported within the execution stage of the flush process`
in prod logs (2026-10-08 13:27 + 13:35).  SQLAlchemy's own docs
promise to turn that warning into a hard error in a future release,
and the queued entry can silently be lost or duplicated depending on
where the flush is in its own loop.

Two checks:
  1. Creating a Lead emits a CREATE AuditEntry via the fixed path,
     with NO 'Session.add() ... during flush' warning in the capture.
  2. Updating that Lead's `client_name` emits an UPDATE AuditEntry
     with the expected `changes_json`, still no warning.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__OPSFLOW_AUDIT_FLUSH_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, User, AuditEntry
    from app.models.user import user_companies
    from sqlalchemy import text

    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    for u in User.query.filter_by(email="__opsflow_audit__@audit.local").all():
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id))
        db.session.delete(u)
    db.session.commit()
    for c in Company.query.filter_by(name=COMPANY_NAME).all():
        _teardown_company_hard(c.id)
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()

    c = Company(name=COMPANY_NAME, base_currency="EGP")
    db.session.add(c); db.session.flush()
    u = User(email="__opsflow_audit__@audit.local",
              full_name="Opsflow Audit",
              password_hash="x")
    db.session.add(u); db.session.flush()
    db.session.commit()
    _STATE["company_id"] = c.id
    _STATE["user_id"] = u.id


def _teardown_company_hard(company_id):
    from app.models import Company
    from sqlalchemy import inspect, text
    db.session.rollback()
    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    insp = inspect(db.engine)
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {col["name"] for col in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id)
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()


def _was_flush_warning(w_list):
    """True if any captured warning mentions the mid-flush Session.add."""
    for w in w_list:
        msg = str(w.message)
        if "Session.add" in msg and "flush" in msg:
            return True
    return False


# ─── 1. Create a Lead -> CREATE AuditEntry, no SAWarning ─────────
@check("1. CREATE AuditEntry emitted via connection, no mid-flush warning")
def _():
    from app.models import Lead, AuditEntry, LeadStatus
    with warnings.catch_warnings(record=True) as w_list:
        warnings.simplefilter("always")
        lead = Lead(
            company_id=_STATE["company_id"],
            client_name="aud-create",
            phone="0100",
            service_needed="x",
            assigned_to_id=_STATE["user_id"],
            status=LeadStatus.NEW_LEAD,
        )
        db.session.add(lead); db.session.commit()
    _STATE["lead_id"] = lead.id
    assert not _was_flush_warning(w_list), \
        "SAWarning about Session.add during flush still fires"
    row = AuditEntry.query.filter_by(
        company_id=_STATE["company_id"],
        entity_type="Lead",
        entity_id=lead.id,
        action="CREATE",
    ).first()
    assert row is not None, "no CREATE AuditEntry was written"
    return f"audit #{row.id} + 0 flush warnings"


# ─── 2. Update the Lead -> UPDATE AuditEntry, no SAWarning ───────
@check("2. UPDATE AuditEntry carries the change diff, no mid-flush warning")
def _():
    from app.models import Lead, AuditEntry
    lead = db.session.get(Lead, _STATE["lead_id"])
    with warnings.catch_warnings(record=True) as w_list:
        warnings.simplefilter("always")
        lead.client_name = "aud-updated"
        db.session.commit()
    assert not _was_flush_warning(w_list), \
        "SAWarning about Session.add during flush fired on UPDATE"
    row = AuditEntry.query.filter_by(
        company_id=_STATE["company_id"],
        entity_type="Lead",
        entity_id=lead.id,
        action="UPDATE",
    ).order_by(AuditEntry.id.desc()).first()
    assert row is not None, "no UPDATE AuditEntry was written"
    assert row.changes_json and "aud-create" in row.changes_json \
        and "aud-updated" in row.changes_json, \
        f"changes_json missing before/after: {row.changes_json!r}"
    return f"audit #{row.id} with before/after diff"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            # Make sure the listeners the fix touched are actually wired —
            # init_audit_listeners is idempotent (`_AUDIT_LISTENERS_REGISTERED`
            # guard) so re-calling here is safe.
            from app.services.opsflow_extras import init_audit_listeners
            init_audit_listeners()
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown_company_hard(_STATE["company_id"])
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
