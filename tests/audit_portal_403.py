#!/usr/bin/env python3
"""MARSOUD-PORTAL-403-FIX — audit for the employee/client portal 403.

The bug: a user on the `employee` role (how HR auto-provisions a normal
staff account — a programmer, an accountant's assistant, anyone with an
Employee row) got a bare Werkzeug 403 the moment they entered the app.
`confine_client_to_portal` in app/__init__.py abort(403)s every endpoint
outside the portal allowlist, and three things fed it:

  1. only `dashboard.index` (/home) was bounced to the portal —
     `dashboard.landing` ("/", the bare domain) fell through to 403;
  2. base.html had no `employee` sidebar branch, so the role fell through
     to the owner sidebar, which rendered links (ملفاتي / الدعم الفني /
     المهام / التقويم / + شركة جديدة) that the gate then 403s;
  3. `help.` + `support.` were missing from the allowlists even though
     every other before_request gate in the same file treats them as
     invariants.

Plus an adjacent loop: an `employee` with no Employee row bounced
/my/account → dashboard → /my/ → … forever.

This audit proves the fix AND that the confinement wasn't over-widened.
Sections:
  A. entry points (the reported symptom)
  B. allowlist additions — reachable, and still correctly scoped
  C. no-Employee-row loop
  D. the sidebar contract (no rendered link may 403) — for EVERY role
  E. confinement invariants that must still hold
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db  # noqa: E402


CHECKS = []
COMPANY_NAME = "__PORTAL_403_AUDIT__"
_STATE = {}

# Roles that render base.html and therefore have a sidebar contract.
ALL_SIDEBAR_ROLES = [
    "owner", "admin", "ceo", "accountant", "hr_manager",
    "sales_manager", "sales_rep", "project_manager", "team_member",
    "viewer", "employee",
]


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _setup():
    from app.models import Company, User, Employee, Plan
    from app.models.user import user_companies

    _teardown()   # wipe any leftovers from an aborted run

    # A deliberately maximal plan: NULL allowed_subitems so
    # enforce_subitem_gating stays out of the way, and EVERY module
    # enabled. This audit is about the confinement gate, not plan gating
    # (which has its own audit) — and a plan with the default empty
    # allowed_modules would make plan_allows() deny nearly every
    # permission, quietly emptying the sidebar the D-checks crawl.
    plan = Plan.query.filter_by(code="__portal403__").first()
    if not plan:
        plan = Plan(code="__portal403__", name="Audit", name_ar="تدقيق",
                    allowed_subitems=None)
        plan.set_modules([
            "accounting", "sales", "inventory", "purchases", "pos", "crm",
            "hr", "reports", "agent", "employee_reports", "manufacturing",
            "evaluations", "insights", "settings",
        ])
        db.session.add(plan)
        db.session.flush()

    co = Company(name=COMPANY_NAME, plan_id=plan.id)
    db.session.add(co)
    db.session.flush()

    users = {}
    for role in ALL_SIDEBAR_ROLES + ["client"]:
        u = User(email=f"__p403_{role}@audit.local", full_name=f"A {role}")
        u.set_password("Passw0rd!audit1")
        db.session.add(u)
        db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=co.id, role=role))
        users[role] = u.id

    # The programmer: `employee` role WITH an Employee row.
    db.session.add(Employee(company_id=co.id, user_id=users["employee"],
                            name="مبرمج", job_title="مبرمج"))

    # Same role, NO Employee row — the redirect-loop case.
    ghost = User(email="__p403_ghost@audit.local", full_name="A ghost")
    ghost.set_password("Passw0rd!audit1")
    db.session.add(ghost)
    db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=ghost.id, company_id=co.id, role="employee"))
    users["ghost"] = ghost.id

    # A second employee, to prove /files/ stays scoped to your own folder.
    other = User(email="__p403_other@audit.local", full_name="A other")
    other.set_password("Passw0rd!audit1")
    db.session.add(other)
    db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=other.id, company_id=co.id, role="employee"))
    db.session.add(Employee(company_id=co.id, user_id=other.id,
                            name="زميل", job_title="مبرمج"))
    users["other"] = other.id

    db.session.commit()
    _STATE["company_id"] = co.id
    _STATE["plan_id"] = plan.id
    _STATE["users"] = users


def _teardown():
    """Generic wipe: every table carrying a company_id, then the company.

    Creating a Company auto-seeds satellites (leave types, warehouse,
    chart of accounts, system roles…). Enumerating them by hand goes stale
    the moment someone adds a table, and a plain `db.session.delete(co)`
    tries to NULL those FKs instead of removing the rows. Walk the
    metadata in reverse dependency order instead.
    """
    from sqlalchemy import text
    from app.models import Company, User, Plan
    from app.models.user import user_companies

    ids = [c.id for c in Company.query.filter_by(name=COMPANY_NAME).all()]
    if ids:
        tables = list(reversed(db.metadata.sorted_tables))
        for t in tables:
            if "company_id" in t.c:
                db.session.execute(
                    t.delete().where(t.c.company_id.in_(ids)))
        db.session.commit()

    for u in User.query.filter(User.email.like("__p403_%@audit.local")).all():
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id))
        # Anything still pointing at this user by FK (files, notifications…)
        for t in reversed(db.metadata.sorted_tables):
            if "user_id" in t.c and t.name != "user_companies":
                db.session.execute(t.delete().where(t.c.user_id == u.id))
        db.session.delete(u)
    db.session.commit()

    for cid in ids:
        db.session.execute(
            text("DELETE FROM companies WHERE id = :i"), {"i": cid})
    for p in Plan.query.filter_by(code="__portal403__").all():
        db.session.delete(p)
    db.session.commit()


# ─── Helpers ────────────────────────────────────────────────────────────
def _client_for(role):
    """Logged-in test client for a fixture role."""
    app = _STATE["app"]
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(_STATE["users"][role])
        s["_fresh"] = True
        s["active_company_id"] = _STATE["company_id"]
    return c


def _get(role, url, follow=False):
    return _client_for(role).get(url, follow_redirects=follow)


def _hrefs(html):
    """Internal GET-safe links rendered on a page."""
    out = set()
    for h in re.findall(r'href="(/[^"]*)"', html):
        h = h.split("#")[0]
        if not h or h.startswith("/static"):
            continue
        if "logout" in h:          # ends the session mid-crawl
            continue
        out.add(h)
    return sorted(out)


def _landing_for(role):
    """Where this role's UI actually lives (crawl entry point)."""
    return "/my/account" if role == "employee" else "/home"


# ═══ A. Entry points — the reported symptom ═════════════════════════════
@check("A1. GET / as `employee` bounces to the portal (was: 403)")
def _():
    r = _get("employee", "/")
    assert r.status_code == 302, f"expected 302, got {r.status_code}"
    assert "/my" in r.headers["Location"], r.headers["Location"]
    return f"302 → {r.headers['Location']}"


@check("A2. GET / as `client` bounces to the portal (was: 403)")
def _():
    r = _get("client", "/")
    assert r.status_code == 302, f"expected 302, got {r.status_code}"
    assert "/portal" in r.headers["Location"], r.headers["Location"]
    return f"302 → {r.headers['Location']}"


@check("A3. GET /home still bounces for both portal roles (no regression)")
def _():
    e = _get("employee", "/home")
    c = _get("client", "/home")
    assert e.status_code == 302 and "/my" in e.headers["Location"]
    assert c.status_code == 302 and "/portal" in c.headers["Location"]
    return "employee → /my/, client → /portal/"


@check("A4. GET / for a NON-portal role still serves the landing page")
def _():
    results = {}
    for role in ("owner", "admin", "accountant", "team_member", "viewer"):
        r = _get(role, "/")
        results[role] = r.status_code
        assert r.status_code == 200, f"{role} got {r.status_code} on /"
    return f"200 for {', '.join(results)}"


@check("A5. the programmer's full entry path terminates on a real page")
def _():
    r = _get("employee", "/", follow=True)
    assert r.status_code == 200, f"ended on {r.status_code}"
    assert len(r.history) >= 2, "expected / → /my/ → /my/account"
    trail = " → ".join([h.headers["Location"] for h in r.history])
    return f"/ → {trail} ⇒ 200"


# ═══ B. Allowlist additions ════════════════════════════════════════════
@check("B1. `support.` reachable for employee + client")
def _():
    for role in ("employee", "client"):
        r = _get(role, "/support/")
        assert r.status_code == 200, f"{role} got {r.status_code}"
    return "200 for both"


@check("B2. `help.` reachable for employee + client")
def _():
    # /help/ index may 404 if nothing is published; what matters is that
    # the confinement gate no longer turns it into a 403.
    for role in ("employee", "client"):
        r = _get(role, "/help/")
        assert r.status_code != 403, f"{role} still 403 on /help/"
    return "no 403 for either role"


@check("B3. `user_files.` reachable for employee, and scoped to OWN folder")
def _():
    r = _get("employee", "/files/")
    assert r.status_code == 200, f"got {r.status_code}"
    # Another user's folder must stay closed — the allowlist widened the
    # blueprint, not the per-file authorization inside it.
    other_id = _STATE["users"]["other"]
    r2 = _get("employee", f"/files/user/{other_id}/")
    assert r2.status_code in (403, 404), \
        f"employee reached another user's folder ({r2.status_code})"
    return f"own folder 200, other user's folder {r2.status_code}"


@check("B4. `user_files.` was NOT opened up for `client`")
def _():
    r = _get("client", "/files/")
    assert r.status_code == 403, f"expected 403, got {r.status_code}"
    return "client still 403 on /files/ (allowlist is per-role)"


# ═══ C. The no-Employee-row loop ═══════════════════════════════════════
@check("C1. employee with NO Employee row: /my/account terminates (was: loop)")
def _():
    c = _client_for("ghost")
    r = c.get("/my/account", follow_redirects=True)
    assert r.status_code == 200, f"got {r.status_code}"
    assert len(r.history) == 0, f"unexpected redirect chain: {r.history}"
    body = r.get_data(as_text=True)
    assert "غير مربوط بسجل موظف" in body, "no_record page not rendered"
    return "200, 0 redirects, terminal page rendered"


@check("C2. …and entering at / doesn't loop either")
def _():
    c = _client_for("ghost")
    r = c.get("/", follow_redirects=True)
    assert r.status_code == 200, f"got {r.status_code}"
    assert len(r.history) <= 4, f"{len(r.history)} redirects — smells like a loop"
    return f"200 after {len(r.history)} redirects"


@check("C3. /my/daily-reports takes the same terminal path")
def _():
    c = _client_for("ghost")
    r = c.get("/my/daily-reports", follow_redirects=True)
    assert r.status_code == 200, f"got {r.status_code}"
    assert len(r.history) == 0, f"redirected: {r.history}"
    return "200, 0 redirects"


@check("C4. non-portal role keeps the ORIGINAL flash+redirect from /my/account")
def _():
    r = _get("admin", "/my/account")
    assert r.status_code == 302, f"expected 302, got {r.status_code}"
    assert "/home" in r.headers["Location"], r.headers["Location"]
    return f"admin still 302 → {r.headers['Location']}"


# ═══ D. Sidebar contract — no rendered link may 403 ════════════════════
@check("D1. employee portal page renders ZERO links that 403")
def _():
    c = _client_for("employee")
    html = c.get("/my/account").get_data(as_text=True)
    links = _hrefs(html)
    assert links, "no links found — did the page render?"
    bad = [h for h in links if c.get(h, follow_redirects=True).status_code == 403]
    assert not bad, f"dead links: {bad}"
    return f"{len(links)} links, 0 forbidden"


@check("D2. EVERY role's landing page renders zero links that 403")
def _():
    report = []
    bad_total = {}
    for role in ALL_SIDEBAR_ROLES:
        c = _client_for(role)
        r = c.get(_landing_for(role), follow_redirects=True)
        if r.status_code != 200:
            report.append(f"{role}:{r.status_code}")
            continue
        links = _hrefs(r.get_data(as_text=True))
        bad = [h for h in links
               if c.get(h, follow_redirects=True).status_code == 403]
        if bad:
            bad_total[role] = bad
        report.append(f"{role}:{len(links)}")
    assert not bad_total, f"dead links per role: {bad_total}"
    return "links checked → " + ", ".join(report)


@check("D3. employee sidebar shows the portal links, not the owner sidebar")
def _():
    html = _get("employee", "/my/account").get_data(as_text=True)
    assert 'href="/my/account"' in html, "حسابي link missing"
    assert 'href="/my/daily-reports"' in html, "daily reports link missing"
    assert 'href="/files/"' in html, "ملفاتي link missing"
    assert 'href="/support/"' in html, "support link missing"
    # Owner-sidebar leakage that used to produce the 403s:
    for leaked in ('href="/journals/"', 'href="/invoices/"',
                   'href="/tasks/"', 'href="/calendar/"'):
        assert leaked not in html, f"owner-sidebar leak: {leaked}"
    return "4 portal links present, 0 owner-sidebar leaks"


@check("D4. «+ شركة جديدة» hidden from portal roles, kept for the rest")
def _():
    NEW_CO = 'href="/companies/new"'
    emp = _get("employee", "/my/account").get_data(as_text=True)
    cli = _get("client", "/portal/", follow=True).get_data(as_text=True)
    own = _get("owner", "/home").get_data(as_text=True)
    assert NEW_CO not in emp, "still shown to employee"
    assert NEW_CO not in cli, "still shown to client"
    assert NEW_CO in own, "wrongly hidden from owner"
    return "hidden for employee+client, present for owner"


@check("D5. AI-agent button hidden from employee, kept for accountant")
def _():
    emp = _get("employee", "/my/account").get_data(as_text=True)
    acc = _get("accountant", "/home").get_data(as_text=True)
    assert "toggleAgent()" not in emp, "agent button still rendered for employee"
    assert "toggleAgent()" in acc, "agent button wrongly hidden from accountant"
    return "hidden for employee, present for accountant"


# ═══ E. Confinement invariants that must still hold ════════════════════
@check("E1. employee still 403s on the financial + business modules")
def _():
    blocked = ["/journals/", "/invoices/", "/accounts/", "/reports/",
               "/vendor-bills/", "/payroll/", "/hr/", "/tasks/",
               "/projects/", "/leads/", "/customers/", "/inventory/",
               "/settings/roles/", "/companies/"]
    leaked = []
    for url in blocked:
        r = _get("employee", url)
        if r.status_code not in (403, 404):
            leaked.append(f"{url}={r.status_code}")
    assert not leaked, f"confinement widened by mistake: {leaked}"
    return f"{len(blocked)} modules still closed"


@check("E2. client still 403s on everything outside its portal")
def _():
    blocked = ["/journals/", "/invoices/", "/tasks/", "/my/account",
               "/files/", "/hr/", "/settings/roles/"]
    leaked = []
    for url in blocked:
        r = _get("client", url)
        if r.status_code not in (403, 404):
            leaked.append(f"{url}={r.status_code}")
    assert not leaked, f"confinement widened by mistake: {leaked}"
    return f"{len(blocked)} routes still closed"


@check("E3. the allowlists are exactly what we intend (no silent drift)")
def _():
    import app as app_pkg
    src = (Path(app_pkg.__file__).parent / "__init__.py").read_text(
        encoding="utf-8")
    # Split on the closing paren at the tuple's own indent — the comments
    # inside the tuples contain parentheses of their own.
    def _tuple_body(name):
        return src.split(f"{name} = (")[1].split("\n    )")[0]
    client_block = _tuple_body("CLIENT_ALLOWED_ENDPOINTS")
    emp_block = _tuple_body("EMPLOYEE_ALLOWED_ENDPOINTS")
    for token in ('"help."', '"support."'):
        assert token in client_block, f"{token} missing from client allowlist"
        assert token in emp_block, f"{token} missing from employee allowlist"
    assert '"user_files."' in emp_block, "user_files. missing from employee"
    assert '"user_files."' not in client_block, "user_files. leaked to client"
    assert "dashboard.landing" in src, "landing bounce missing"
    return "help./support. on both, user_files. employee-only, landing bounced"


@check("E4. before_request hook order preserved (gate still runs last)")
def _():
    app = _STATE["app"]
    names = [f.__name__ for f in app.before_request_funcs[None]]
    assert "confine_client_to_portal" in names, "gate not registered"
    assert names.index("load_active_company") < \
        names.index("confine_client_to_portal"), \
        "confinement runs before the company is resolved"
    return " → ".join(names[:3]) + f" … ({len(names)} hooks)"


@check("E6. insights carve-out: read-only agent open, posting agent closed")
def _():
    # The three roles permissions.py grants insights.use to reach the
    # read-only analyst…
    for role in ("hr_manager", "sales_manager", "project_manager"):
        r = _get(role, "/agent/insights")
        assert r.status_code == 200, \
            f"{role} got {r.status_code} on /agent/insights"
    # …while the journal-posting accountant agent stays hard-blocked for
    # every non-financial role (the reason `agent.` is in the list).
    blocked = []
    for role in ("hr_manager", "sales_manager", "sales_rep",
                 "project_manager", "team_member"):
        for url in ("/agent/", "/agent/chat"):
            code = _client_for(role).post(url).status_code \
                if url.endswith("chat") else _get(role, url).status_code
            if code != 403:
                blocked.append(f"{role}{url}={code}")
    assert not blocked, f"accountant agent leaked: {blocked}"
    # A role WITHOUT insights.use is still stopped — by the route's own
    # permission gate (redirect + flash), not by the prefix block.
    r = _get("team_member", "/agent/insights")
    assert r.status_code == 302, f"team_member got {r.status_code}"
    return "3 roles reach /agent/insights, agent.chat/index still 403"


@check("E7. agent button renders iff the role actually has agent.use")
def _():
    # D1/D2 crawl href="" links; the AI-agent launcher is an onclick, so it
    # slipped past them. It used to render for hr_manager / sales_manager /
    # sales_rep / project_manager (403 on agent.chat via the financial
    # block) and ceo / viewer (no agent.use grant → redirect) — a control
    # that could only ever fail. base.html now keys off has_permission.
    from app.services.permissions import has_permission
    from app.models import User, Company
    # Read the permissions inside a SHORT-lived app context, then drop it
    # before issuing any request — holding one across test-client calls is
    # what made the first version of this audit test one role eleven times.
    allowed = {}
    with _STATE["app"].app_context():
        company = db.session.get(Company, _STATE["company_id"])
        for role in ALL_SIDEBAR_ROLES:
            user = db.session.get(User, _STATE["users"][role])
            allowed[role] = has_permission("agent.use", user=user,
                                           company=company)
    mismatched = []
    for role in ALL_SIDEBAR_ROLES:
        r = _client_for(role).get(_landing_for(role), follow_redirects=True)
        rendered = 'onclick="toggleAgent()"' in r.get_data(as_text=True)
        if rendered != allowed[role]:
            mismatched.append(
                f"{role}: button={rendered} but agent.use={allowed[role]}")
    assert not mismatched, f"dead/missing agent buttons: {mismatched}"
    yes = sorted(r for r in ALL_SIDEBAR_ROLES if allowed[r])
    return f"{len(ALL_SIDEBAR_ROLES)} roles checked; button only for {yes}"


@check("E5. every URL rule still builds (no template/url_for breakage)")
def _():
    app = _STATE["app"]
    n = len(list(app.url_map.iter_rules()))
    assert n > 200, f"only {n} rules — blueprints failed to register?"
    for tpl in ("portal_emp/no_record.html", "portal_emp/account.html",
                "base.html"):
        app.jinja_env.get_template(tpl)
    return f"{n} routes, 3 touched templates parse"


# ─── Run ────────────────────────────────────────────────────────────────
def _preflight_session(app):
    """Abort loudly if the fixture sessions don't authenticate.

    Every assertion below distinguishes 403 (blocked) from 302 (allowed
    elsewhere) — so an UNauthenticated test client turns this audit into a
    liar. confine_client_to_portal returns early when the user is
    anonymous, @login_required then bounces every route to /login, and the
    run reports things like "confinement widened: /journals/=302" — which
    reads as a security regression when in fact the gate never ran. That
    misdiagnosis has cost a revert once already; fail with the real cause
    instead of 17 misleading failures.

    The usual trigger is a production-style .env: SESSION_COOKIE_DOMAIN
    =.marsoud.com scopes the cookie to that domain, and the test client
    runs on localhost, so it is never sent (see the MARSOUD-SESSION-
    COOKIE-DEV-FIX note in config.py). It is irrelevant to what this audit
    exercises, so we neutralise it for the run rather than depend on which
    .env happens to be on the machine.
    """
    domain = app.config.get("SESSION_COOKIE_DOMAIN")
    if domain:
        app.config["SESSION_COOKIE_DOMAIN"] = None
        print(f"NOTE  SESSION_COOKIE_DOMAIN={domain!r} overridden to None "
              f"for this run\n      (a domain-scoped cookie is never sent "
              f"to the localhost test client).")
    r = _client_for("owner").get("/home", follow_redirects=False)
    landed = r.headers.get("Location", "") if r.status_code in (301, 302) else ""
    if "/login" in landed:
        print("\nABORT  fixture session is NOT authenticated — GET /home as "
              "the owner redirected to /login.")
        print("       Every 403-vs-302 assertion below would be meaningless, "
               "so the run is stopping here.")
        print("       This is an environment problem, not a code failure. "
              "Check SESSION_COOKIE_DOMAIN,")
        print("       SECRET_KEY stability, and that Flask-Login is wired "
              "before re-reading any result.")
        return False
    return True


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    _STATE["app"] = app
    passed = failed = 0

    # NB: the checks deliberately run with NO app context pushed.
    # Flask reuses an already-pushed app context for test-client requests
    # instead of creating a fresh one, and Flask-Login caches the resolved
    # user on `g._login_user`. Wrapping the whole run in
    # `with app.app_context():` (the habit in the older audits here) makes
    # every request after the first answer as the FIRST role logged in —
    # multi-role assertions then silently test one role thirteen times.
    with app.app_context():
        _setup()
    try:
        if not _preflight_session(app):
            with app.app_context():
                _teardown()
            print("\n────  aborted before any check ran  ────")
            sys.exit(2)
        for label, fn in CHECKS:
            try:
                result = fn()
                print(f"PASS  {label}\n        ⇒ {result}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback
                traceback.print_exc()
    finally:
        try:
            with app.app_context():
                _teardown()
            print("\n(fixture company + users cleaned up)")
        except Exception as e:  # noqa: BLE001
            print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
