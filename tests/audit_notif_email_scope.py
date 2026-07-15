#!/usr/bin/env python3
"""MARSOUD-NOTIF-EMAIL-SCOPE (Abdelhamid 2026-07-15).

"عاوزين الاشعارات في المنشن والانشاء بس" — Abdelhamid wants email
notifications for TWO events only:
  1. Task creation / assignment (TASK_ASSIGNED)
  2. @-mention in a comment (handled by app/services/mentions.py)

Everything else should stay IN-APP only via the bell:
  · TASK_STATUS_CHANGED
  · TASK_COMMENT
  · TASK_UPDATED
  · Anything else

Checks:
  1. TASK_ASSIGNED still triggers an email (positive case).
  2. TASK_COMMENT does NOT trigger an email but DOES insert an
     in-app Notification row.
  3. TASK_STATUS_CHANGED does NOT trigger an email; still logs bell.
  4. TASK_UPDATED does NOT trigger an email; still logs bell.
  5. Mention path (services.mentions) still emails independently —
     regression guard so the follow-up ticket doesn't silence
     the mention path by accident.
"""
import sys
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
            "DELETE FROM users WHERE email LIKE 'nes-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Task, TaskStatus, TaskPriority,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__NES__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__NES__", base_currency="SAR")
    db.session.add(a); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id)

    def _mk(email, role):
        u = User(email=email,
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=a.id, role=role))
        return u

    owner = _mk("nes-owner@x.test", "owner")
    recipient = _mk("nes-recipient@x.test", "sales_rep")

    t = Task(
        company_id=a.id, title="NES task",
        assigned_to_id=recipient.id, created_by_id=owner.id,
        priority=TaskPriority.MEDIUM, status=TaskStatus.TODO,
    )
    db.session.add(t); db.session.commit()

    _STATE.update(
        a_id=a.id, owner_id=owner.id, recipient_id=recipient.id,
        task_id=t.id,
    )


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _capture_email_calls(func):
    """Run func with app.services.email.send_task_notification_email
    patched to record how many times it was called. Also patches
    services.email.send_email for the mention path. Returns
    (result, task_email_calls, generic_email_calls)."""
    from app.services import email as email_mod
    from app.services import tasks_extras as te_mod
    from app.services import mentions as mentions_mod
    task_calls = []
    generic_calls = []

    def _task_stub(recipient, task, *, kind, title=None, body_text=None):
        task_calls.append((kind, getattr(recipient, "email", None)))

    def _generic_stub(to, subject, html_body, **kw):
        generic_calls.append((to, subject))

    # Patch the imports at the point of use. tasks_extras.py does
    # `from app.services.email import send_task_notification_email`
    # INSIDE _notify at call time, so patching the module attribute
    # is enough — the local import re-reads from the module.
    real_task = email_mod.send_task_notification_email
    real_generic = email_mod.send_email
    email_mod.send_task_notification_email = _task_stub
    email_mod.send_email = _generic_stub
    try:
        result = func()
    finally:
        email_mod.send_task_notification_email = real_task
        email_mod.send_email = real_generic
    return result, task_calls, generic_calls


# ─── Positive: TASK_ASSIGNED emails ───────────────────────────────
@check("1. TASK_ASSIGNED still triggers an email (creation flow)")
def _():
    from app.services.tasks_extras import _notify
    from app.models import NotificationKind, Task
    task = db.session.get(Task, _STATE["task_id"])
    _, calls, _generic = _capture_email_calls(lambda: _notify(
        _STATE["recipient_id"], company_id=_STATE["a_id"],
        kind=NotificationKind.TASK_ASSIGNED,
        title="assigned", body="body", link_url="/tasks/x",
        task=task,
    ))
    db.session.commit()
    assert len(calls) == 1, f"expected 1 email, got {len(calls)}"
    assert calls[0][0] == "TASK_ASSIGNED"
    return "TASK_ASSIGNED → 1 email"


# ─── Negative: other kinds don't email ───────────────────────────
@check("2. TASK_COMMENT does NOT email; still creates in-app notification")
def _():
    from app.services.tasks_extras import _notify
    from app.models import NotificationKind, Notification, Task
    task = db.session.get(Task, _STATE["task_id"])
    before = Notification.query.filter_by(
        user_id=_STATE["recipient_id"], kind="TASK_COMMENT",
    ).count()
    _, calls, _generic = _capture_email_calls(lambda: _notify(
        _STATE["recipient_id"], company_id=_STATE["a_id"],
        kind=NotificationKind.TASK_COMMENT,
        title="new comment", body="body", link_url="/tasks/x",
        task=task,
    ))
    db.session.commit()
    assert len(calls) == 0, f"COMMENT sent {len(calls)} emails"
    after = Notification.query.filter_by(
        user_id=_STATE["recipient_id"], kind="TASK_COMMENT",
    ).count()
    assert after == before + 1, \
        f"bell notification not inserted: {before} → {after}"
    return "COMMENT: 0 emails, bell +1"


@check("3. TASK_STATUS_CHANGED does NOT email; bell still fires")
def _():
    from app.services.tasks_extras import _notify
    from app.models import NotificationKind, Notification, Task
    task = db.session.get(Task, _STATE["task_id"])
    before = Notification.query.filter_by(
        user_id=_STATE["recipient_id"], kind="TASK_STATUS_CHANGED",
    ).count()
    _, calls, _generic = _capture_email_calls(lambda: _notify(
        _STATE["recipient_id"], company_id=_STATE["a_id"],
        kind=NotificationKind.TASK_STATUS_CHANGED,
        title="moved", body="body", link_url="/tasks/x",
        task=task,
    ))
    db.session.commit()
    assert len(calls) == 0, f"STATUS sent {len(calls)} emails"
    after = Notification.query.filter_by(
        user_id=_STATE["recipient_id"], kind="TASK_STATUS_CHANGED",
    ).count()
    assert after == before + 1
    return "STATUS: 0 emails, bell +1"


@check("4. TASK_UPDATED does NOT email; bell still fires")
def _():
    from app.services.tasks_extras import _notify
    from app.models import NotificationKind, Notification, Task
    task = db.session.get(Task, _STATE["task_id"])
    before = Notification.query.filter_by(
        user_id=_STATE["recipient_id"], kind="TASK_UPDATED",
    ).count()
    _, calls, _generic = _capture_email_calls(lambda: _notify(
        _STATE["recipient_id"], company_id=_STATE["a_id"],
        kind=NotificationKind.TASK_UPDATED,
        title="updated", body="body", link_url="/tasks/x",
        task=task,
    ))
    db.session.commit()
    assert len(calls) == 0, f"UPDATED sent {len(calls)} emails"
    after = Notification.query.filter_by(
        user_id=_STATE["recipient_id"], kind="TASK_UPDATED",
    ).count()
    assert after == before + 1
    return "UPDATED: 0 emails, bell +1"


# ─── Mention path stays alive ────────────────────────────────────
@check("5. Mention path still fires an email (independent code path)")
def _():
    from app.services.mentions import notify_mentions
    _, task_calls, generic_calls = _capture_email_calls(lambda: notify_mentions(
        actor_user_id=_STATE["owner_id"],
        mentioned_user_ids={_STATE["recipient_id"]},
        company_id=_STATE["a_id"],
        entity_kind="task", entity_label="مهمة test",
        link_url="/tasks/1", snippet="hello @recipient",
    ))
    db.session.commit()
    # Mentions go through send_email (not send_task_notification_email).
    assert len(generic_calls) >= 1, \
        f"mention email path silent: {generic_calls}"
    return f"mention email fired ({len(generic_calls)} call(s))"


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
                if "a_id" in _STATE:
                    _teardown(_STATE["a_id"])
                print("\n(cleaned up fixture company)")
            except Exception as e:
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
