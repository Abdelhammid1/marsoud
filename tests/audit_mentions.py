#!/usr/bin/env python3
"""MARSOUD-MENTIONS — audit for @-mention plumbing in comments.

Coverage:
  1. parse_mention_ids extracts user IDs from `@[Name](user:ID)` tokens.
  2. parse_mention_ids dedupes when the same user is mentioned twice.
  3. parse_mention_ids returns an empty set for plain-text / None.
  4. notify_mentions fires one MENTION notification per unique user.
  5. notify_mentions skips the actor themselves (no self-ping).
  6. notify_mentions drops users who don't belong to the company
     (defense against a hand-crafted token pointing at a stranger).
  7. render_mentions escapes HTML in the display name (XSS defense).
  8. render_mentions produces an anchor tag pointing at
     /tasks/?scope=employees&user_id=<ID>.
  9. /users/api/search returns members of the current company only
     (cross-tenant safety).
 10. Full end-to-end: POST a task comment with @[Name](user:ID) →
     saved comment + MENTION notification for the mentioned user.
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


def _setup():
    from app.models import (
        Company, User, user_companies, Task, TaskStatus, TaskPriority,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__MENTIONS_A__", "__MENTIONS_B__"):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__MENTIONS_A__", base_currency="SAR")
    b = Company(name="__MENTIONS_B__", base_currency="SAR")
    db.session.add_all([a, b]); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(a.id); seed_default_coa(b.id)

    def _mk(email, company_id):
        u = User(email=email,
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=company_id, role="owner"))
        return u

    actor = _mk("mention-actor@x.test", a.id)
    victim = _mk("mention-victim@x.test", a.id)
    peer = _mk("mention-peer@x.test", a.id)
    outsider = _mk("mention-outsider@x.test", b.id)

    t = Task(
        company_id=a.id, title="Mention target",
        assigned_to_id=actor.id, created_by_id=actor.id,
        priority=TaskPriority.MEDIUM, status=TaskStatus.TODO,
    )
    db.session.add(t); db.session.commit()
    _STATE.update(
        a_id=a.id, b_id=b.id,
        actor_id=actor.id, victim_id=victim.id, peer_id=peer.id,
        outsider_id=outsider.id, task_id=t.id,
    )


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM task_assignees WHERE task_id IN "
            "(SELECT id FROM tasks WHERE company_id = :c)"
        ), {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                             {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'mention-%@x.test'"))


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
                "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


# ─── Parser checks ────────────────────────────────────────────────────
@check("1. parse_mention_ids extracts user IDs")
def _():
    from app.services.mentions import parse_mention_ids
    text = "hi @[Alice](user:42) and @[Bob](user:17), please review."
    ids = parse_mention_ids(text)
    assert ids == {42, 17}
    return "42, 17 extracted"


@check("2. parse_mention_ids dedupes repeated mentions")
def _():
    from app.services.mentions import parse_mention_ids
    text = "@[Alice](user:42) again @[Alice](user:42) triple @[Alice](user:42)"
    assert parse_mention_ids(text) == {42}
    return "single ID from 3× mention"


@check("3. parse_mention_ids returns empty set for plain text / None")
def _():
    from app.services.mentions import parse_mention_ids
    assert parse_mention_ids(None) == set()
    assert parse_mention_ids("") == set()
    assert parse_mention_ids("hey there no mentions here") == set()
    # Malformed tokens (missing user id, non-numeric) → ignored
    assert parse_mention_ids("@[Bob](user:notnum)") == set()
    return "3 empty inputs handled + malformed ignored"


# ─── Notify checks ────────────────────────────────────────────────────
@check("4. notify_mentions creates one MENTION per unique user")
def _():
    from app.services.mentions import notify_mentions
    from app.models import Notification
    _clear_notifs()
    n = notify_mentions(
        actor_user_id=_STATE["actor_id"],
        mentioned_user_ids={_STATE["victim_id"], _STATE["peer_id"]},
        company_id=_STATE["a_id"],
        entity_kind="task", entity_label="مهمة: تجربة",
        link_url="/tasks/1", snippet="check this",
    )
    assert n == 2, f"expected 2, got {n}"
    # Exactly one notification per user
    for uid in (_STATE["victim_id"], _STATE["peer_id"]):
        rows = Notification.query.filter_by(
            company_id=_STATE["a_id"], user_id=uid,
            kind="MENTION",
        ).count()
        assert rows == 1, f"user {uid} got {rows} rows"
    return "2 notifications sent, one per user"


@check("5. notify_mentions silently skips the actor (no self-ping)")
def _():
    from app.services.mentions import notify_mentions
    from app.models import Notification
    _clear_notifs()
    n = notify_mentions(
        actor_user_id=_STATE["actor_id"],
        mentioned_user_ids={_STATE["actor_id"], _STATE["victim_id"]},
        company_id=_STATE["a_id"],
        entity_kind="task", entity_label="مهمة",
        link_url="/tasks/1", snippet="",
    )
    assert n == 1
    self_rows = Notification.query.filter_by(
        user_id=_STATE["actor_id"], kind="MENTION",
    ).count()
    assert self_rows == 0
    return "no self-ping; other user notified"


@check("6. notify_mentions drops users outside the company")
def _():
    """A hand-crafted mention pointing at a user_id who happens to
    exist but isn't a member of this company must be silently
    dropped — otherwise the mention becomes a cross-tenant leak."""
    from app.services.mentions import notify_mentions
    from app.models import Notification
    _clear_notifs()
    n = notify_mentions(
        actor_user_id=_STATE["actor_id"],
        mentioned_user_ids={_STATE["outsider_id"], _STATE["victim_id"]},
        company_id=_STATE["a_id"],
        entity_kind="task", entity_label="مهمة",
        link_url="/tasks/1", snippet="",
    )
    assert n == 1
    outsider_rows = Notification.query.filter_by(
        user_id=_STATE["outsider_id"], kind="MENTION",
    ).count()
    assert outsider_rows == 0
    return "outsider dropped; company member notified"


def _clear_notifs():
    from sqlalchemy import text as _t
    with db.engine.begin() as conn:
        conn.execute(_t(
            "DELETE FROM notifications WHERE company_id IN (:a, :b)"
        ), {"a": _STATE["a_id"], "b": _STATE["b_id"]})


# ─── Renderer checks ──────────────────────────────────────────────────
@check("7. render_mentions escapes HTML in the display name (XSS)")
def _():
    """A user whose display name happens to contain HTML (or one
    crafted by an attacker via a hand-crafted mention) must NOT
    inject markup into the rendered comment."""
    from app.services.mentions import render_mentions
    out = render_mentions('@[<script>alert(1)</script>](user:5)')
    assert "<script>alert" not in str(out)
    assert "&lt;script&gt;" in str(out)
    return "HTML escaped"


@check("8. render_mentions emits anchor pointing at the user's tasks")
def _():
    from app.services.mentions import render_mentions
    out = str(render_mentions("hi @[Alice](user:42)"))
    assert 'user_id=42' in out, f"link missing user_id: {out}"
    assert '<a ' in out
    assert '@Alice' in out
    return "anchor + @Alice text present"


# ─── API + full round-trip checks ─────────────────────────────────────
@check("9. /users/api/search scoped to current company")
def _():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["actor_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    r = client.get("/users/api/search?q=mention")
    assert r.status_code == 200
    data = r.get_json()
    ids = {row["id"] for row in data}
    # actor, victim, peer are all in company A → all in the results
    for uid in (_STATE["actor_id"], _STATE["victim_id"], _STATE["peer_id"]):
        assert uid in ids, f"user {uid} missing from search"
    # outsider is in company B → must NOT appear
    assert _STATE["outsider_id"] not in ids, "outsider leaked into search"
    return f"{len(ids)} rows, no cross-tenant leak"


@check("10. POST task comment with @-mention fires notification end-to-end")
def _():
    from flask import current_app
    from app.models import Notification, TaskComment
    _clear_notifs()
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["actor_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    text = f"Please review @[Victim](user:{_STATE['victim_id']}) thanks"
    r = client.post(
        f"/tasks/{_STATE['task_id']}/comments",
        data={"content": text},
        follow_redirects=False,
    )
    assert r.status_code in (200, 302), f"status={r.status_code}"
    # Comment persisted
    c = TaskComment.query.filter_by(task_id=_STATE["task_id"]).first()
    assert c is not None, "comment not created"
    assert "@[Victim]" in c.content
    # Notification fired
    n = Notification.query.filter_by(
        user_id=_STATE["victim_id"], kind="MENTION",
    ).first()
    assert n is not None, "no MENTION notification created"
    assert f"/tasks/{_STATE['task_id']}" in (n.link_url or ""), \
        f"wrong link_url: {n.link_url}"
    return f"comment + notification round-trip OK"


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
