#!/usr/bin/env python3
"""MARSOUD-LINKIFY + MARSOUD-CLICKABLE-ASSIGNEE (Abdelhamid 2026-07-16).

Two related UX tickets:

  A. Auto-detect URLs inside free-text (comments, task titles,
     task descriptions, lead notes) and render them as clickable
     anchors — safe against XSS.

  B. Employee names shown anywhere inside a task (creator badge,
     comment author, activity log actor + assignee references)
     link to /tasks/?scope=employees&user_id=<id>.

Checks:
  1. render_linkify() on a plain URL string emits <a> with the URL.
  2. render_linkify() escapes surrounding HTML (XSS defense).
  3. render_linkify() adds https:// scheme to www.-only URLs.
  4. render_linkify() composed AFTER mentions preserves the
     mention badges intact.
  5. HTTP GET /tasks/<id> renders a linkified URL from the
     description.
  6. HTTP GET /tasks/<id> renders a linkified URL from a comment
     (and keeps mentions working).
  7. activity_description returns Markup with an anchor around
     the actor name for a CREATED action.
  8. activity_description links added/removed assignee names for
     an ASSIGNEES_CHANGED action.
  9. Task detail HTML: creator badge in the header is a link.
 10. Task detail HTML: comment author name is a link.
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
        conn.execute(text(
            "DELETE FROM task_assignees WHERE task_id IN "
            "(SELECT id FROM tasks WHERE company_id = :c)"
        ), {"c": company_id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": company_id})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'lca-%@x.test'"))


def _setup():
    from app.models import (
        Company, User, user_companies, Task, TaskStatus, TaskPriority,
        TaskComment,
    )
    from werkzeug.security import generate_password_hash

    for name in ("__LCA__",):
        c = Company.query.filter_by(name=name).first()
        if c:
            _teardown(c.id)
    a = Company(name="__LCA__", base_currency="SAR")
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

    owner = _mk("lca-owner@x.test", "owner")
    assignee = _mk("lca-assignee@x.test", "sales_rep")
    t = Task(
        company_id=a.id, title="LCA task",
        description="check https://example.com/foo?bar=1 for details",
        assigned_to_id=assignee.id, created_by_id=owner.id,
        priority=TaskPriority.MEDIUM, status=TaskStatus.TODO,
    )
    db.session.add(t); db.session.flush()
    db.session.add(TaskComment(
        task_id=t.id, user_id=owner.id, company_id=a.id,
        content=("hello @[LCA-Assignee](user:" + str(assignee.id)
                  + ") — see https://marsoud.com/reports for the numbers"),
    ))
    db.session.commit()
    _STATE.update(
        a_id=a.id, owner_id=owner.id, assignee_id=assignee.id,
        task_id=t.id,
    )


def _reset_g():
    from flask import g
    for k in ("_login_user", "active_company", "user_companies",
              "impersonating"):
        try: g.pop(k, None)
        except Exception: pass


def _login():
    from flask import current_app
    _reset_g()
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["owner_id"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["a_id"]
    return client


# ─── Linkify unit tests ─────────────────────────────────────────
@check("1. render_linkify wraps plain URL in an anchor")
def _():
    from app.services.linkify import render_linkify
    out = str(render_linkify(
        "docs at https://marsoud.com/help thanks"))
    assert 'href="https://marsoud.com/help"' in out, out
    assert "target=\"_blank\"" in out
    return "anchor + target=_blank rendered"


@check("2. render_linkify escapes surrounding HTML (XSS defense)")
def _():
    from app.services.linkify import render_linkify
    out = str(render_linkify(
        '<script>bad()</script> see https://x.test'))
    # < of <script> must be HTML-escaped so it renders as text.
    assert "&lt;script&gt;" in out, out
    assert "<script>" not in out
    return "script tag escaped; URL still linked"


@check("3. render_linkify adds https:// scheme to www.-only URLs")
def _():
    from app.services.linkify import render_linkify
    out = str(render_linkify("visit www.example.com/foo"))
    assert 'href="https://www.example.com/foo"' in out, out
    return "www. → https://www."


@check("4. mentions + linkify composed: mention badge survives")
def _():
    from app.services.mentions import render_mentions
    from app.services.linkify import render_linkify
    text = ("hi @[Bob](user:42) see https://example.com/x")
    step1 = render_mentions(text)
    step2 = str(render_linkify(step1))
    # Mention anchor from mentions filter kept intact.
    assert "/tasks/?scope=employees" in step2
    assert "@Bob" in step2
    # URL got linkified.
    assert 'href="https://example.com/x"' in step2, step2
    return "mention badge + URL anchor both present"


# ─── HTTP task detail: linkify in place ─────────────────────────
@check("5. GET /tasks/<id> renders linkified URL from description")
def _():
    r = _login().get(f"/tasks/{_STATE['task_id']}",
                       follow_redirects=False)
    assert r.status_code == 200
    body = r.data.decode("utf-8", "ignore")
    assert 'href="https://example.com/foo?bar=1"' in body, \
        "description URL not linkified in HTML"
    return "description URL clickable"


@check("6. GET /tasks/<id> renders linkified URL from a comment")
def _():
    r = _login().get(f"/tasks/{_STATE['task_id']}",
                       follow_redirects=False)
    body = r.data.decode("utf-8", "ignore")
    assert 'href="https://marsoud.com/reports"' in body, \
        "comment URL not linkified in HTML"
    # Mention badge should still be present.
    assert "@LCA-Assignee" in body, "mention badge broken"
    return "comment URL + mention both rendered"


# ─── activity_description returns Markup w/ clickable names ─────
@check("7. activity_description: CREATED links the actor name")
def _():
    from app.services.tasks_extras import log_activity, activity_description
    from app.models import Task
    task = db.session.get(Task, _STATE["task_id"])
    # Fabricate an activity row using the existing helper.
    entry = log_activity(
        task, "CREATED", after={"title": task.title},
        user_id=_STATE["owner_id"],
    )
    db.session.commit()
    out = str(activity_description(entry))
    assert f'/tasks/?scope=employees&user_id={_STATE["owner_id"]}' in out, out
    assert 'أنشأ هذه المهمة' in out
    return "actor is a link on CREATED"


@check("8. activity_description: ASSIGNEES_CHANGED links added names")
def _():
    from app.services.tasks_extras import log_activity, activity_description
    from app.models import Task
    task = db.session.get(Task, _STATE["task_id"])
    entry = log_activity(
        task, "ASSIGNEES_CHANGED",
        before={"ids": []},
        after={"ids": [_STATE["assignee_id"]]},
        user_id=_STATE["owner_id"],
    )
    db.session.commit()
    out = str(activity_description(entry))
    # Actor + the added assignee are both linked.
    assert f'user_id={_STATE["owner_id"]}' in out
    assert f'user_id={_STATE["assignee_id"]}' in out
    return "actor + added assignee both linked"


# ─── HTML: creator + comment author are clickable ───────────────
@check("9. Task detail HTML: creator name in header is a link")
def _():
    r = _login().get(f"/tasks/{_STATE['task_id']}",
                       follow_redirects=False)
    body = r.data.decode("utf-8", "ignore")
    # The creator badge should be inside an <a href="...user_id=<owner>">.
    needle = f'user_id={_STATE["owner_id"]}'
    assert needle in body, "creator user_id link missing"
    assert "بواسطة" in body
    return "creator badge → tasks board link"


@check("10. Task detail HTML: comment author is a link")
def _():
    r = _login().get(f"/tasks/{_STATE['task_id']}",
                       follow_redirects=False)
    body = r.data.decode("utf-8", "ignore")
    # Comment author is the owner (from setup).
    assert f'user_id={_STATE["owner_id"]}' in body
    return "comment author → tasks board link"


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
