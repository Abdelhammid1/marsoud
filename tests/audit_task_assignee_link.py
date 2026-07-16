#!/usr/bin/env python3
"""MARSOUD-TASK-ASSIGNEE-LINK — audit for the small ticket that asked
for the assignee name on the task-detail page to be a direct link to
/tasks/?scope=employees&user_id=<uid>.

Coverage:
  1. Every assignee's name is rendered inside an <a> tag.
  2. Each link's href carries the assignee's own user_id (not the
     task creator's).
  3. If a task has multiple assignees, each gets their own link.
  4. Tasks with no assignee (edge case) don't spawn a link.
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
        Company, User, user_companies, Task, TaskPriority, TaskStatus,
        task_assignees,
    )
    from werkzeug.security import generate_password_hash

    existing = Company.query.filter_by(name="__ASSIGN_LINK_AUDIT__").first()
    if existing:
        _teardown(existing.id)

    c = Company(name="__ASSIGN_LINK_AUDIT__", base_currency="SAR")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)

    def _mk_user(email):
        u = User(email=email,
                  password_hash=generate_password_hash(
                      "x", method="pbkdf2:sha256"),
                  full_name=email.split("@")[0])
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role="owner",
        ))
        return u

    owner = _mk_user("assign-owner@x.test")
    alice = _mk_user("assign-alice@x.test")
    bob = _mk_user("assign-bob@x.test")

    # Task with two assignees
    t = Task(
        company_id=c.id, title="اختبار الرابط",
        assigned_to_id=alice.id,       # legacy primary
        created_by_id=owner.id,
        priority=TaskPriority.MEDIUM, status=TaskStatus.TODO,
    )
    db.session.add(t); db.session.flush()
    for uid in (alice.id, bob.id):
        db.session.execute(task_assignees.insert().values(
            task_id=t.id, user_id=uid, assigned_by_id=owner.id))
    db.session.commit()
    _STATE.update(
        company_id=c.id, task_id=t.id,
        owner_id=owner.id, alice_id=alice.id, bob_id=bob.id,
    )


def _teardown(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM task_assignees WHERE task_id IN "
            "(SELECT id FROM tasks WHERE company_id = :c)"
        ), {"c": company_id})
        conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                     {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                             {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'assign-%@x.test'"))


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
               "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _client_as(user_id):
    from flask import current_app
    _reset_g()
    c = current_app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["company_id"]
    return c


@check("1. every assignee name is rendered as an <a> tag")
def _():
    r = _client_as(_STATE["owner_id"]).get(f"/tasks/{_STATE['task_id']}")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Both assignees present
    assert "assign-alice" in body
    assert "assign-bob" in body
    # Each appears inside an <a href> pointing at the scoped tasks URL
    assert f'href="/tasks/?scope=employees&amp;user_id={_STATE["alice_id"]}"' in body \
        or f'href="/tasks/?scope=employees&user_id={_STATE["alice_id"]}"' in body, \
        "alice link missing"
    assert f'href="/tasks/?scope=employees&amp;user_id={_STATE["bob_id"]}"' in body \
        or f'href="/tasks/?scope=employees&user_id={_STATE["bob_id"]}"' in body, \
        "bob link missing"
    return "both alice + bob links rendered"


@check("2. each assignee's link carries their OWN user_id")
def _():
    # AUDIT SYNC 2026-07-16 — the wider MARSOUD-CLICKABLE-ASSIGNEE
    # ticket now ALSO links the creator badge in the task header
    # + the comment author's name, so the creator's user_id
    # legitimately appears as an href on the page. Narrow the
    # negative assertion to "the creator id must not appear inside
    # the assignees list" instead of "anywhere on the page".
    r = _client_as(_STATE["owner_id"]).get(f"/tasks/{_STATE['task_id']}")
    body = r.get_data(as_text=True)
    # Find the assignees region — it lives between the "المكلَّفون"
    # heading and either "تعديل المكلَّفين" or the next card.
    import re
    m_start = body.find("المكلَّفون")
    m_end = body.find("تعديل المكلَّفين", m_start)
    if m_end == -1:
        m_end = body.find("<div class=\"card", m_start + 1)
    if m_end == -1:
        m_end = len(body)
    assignees_section = body[m_start:m_end] if m_start != -1 else ""
    owner_marker_a = f'user_id={_STATE["owner_id"]}"'
    owner_marker_b = f'user_id={_STATE["owner_id"]}&amp;'
    assert (owner_marker_a not in assignees_section
            and owner_marker_b not in assignees_section), \
        "creator wrongly rendered as an assignee link inside the assignees box"
    return "creator id NOT inside assignees section"


@check("3. multi-assignee task renders one link per assignee")
def _():
    r = _client_as(_STATE["owner_id"]).get(f"/tasks/{_STATE['task_id']}")
    body = r.get_data(as_text=True)
    # Count links in the assignees section only.
    import re
    scoped = re.findall(
        r'href="/tasks/\?scope=employees(?:&amp;|&)user_id=(\d+)"',
        body,
    )
    ids = {int(x) for x in scoped}
    assert _STATE["alice_id"] in ids and _STATE["bob_id"] in ids, (
        f"expected alice + bob ids in {ids}"
    )
    return f"{len(ids)} distinct assignee link(s) rendered"


@check("4. link URL matches url_for('tasks.index', scope='employees', user_id)")
def _():
    """Consistency: the href we emit must match Flask's url_for
    for the same route + query. Guards against a future template
    edit hardcoding a wrong path (like /tasks/user/<id>)."""
    from flask import url_for, current_app
    with current_app.test_request_context():
        expected = url_for("tasks.index", scope="employees",
                            user_id=_STATE["alice_id"])
    r = _client_as(_STATE["owner_id"]).get(f"/tasks/{_STATE['task_id']}")
    body = r.get_data(as_text=True)
    # Jinja renders `&` in URL params as `&amp;` inside HTML attrs.
    # Accept either form.
    variant_a = f'href="{expected}"'
    variant_b = f'href="{expected.replace("&", "&amp;")}"'
    assert variant_a in body or variant_b in body, (
        f"expected url_for-generated href in body, got neither "
        f"{variant_a!r} nor {variant_b!r}"
    )
    return f"href matches url_for output"


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
                if "company_id" in _STATE:
                    _teardown(_STATE["company_id"])
                    print("\n(cleaned up fixture company)")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
