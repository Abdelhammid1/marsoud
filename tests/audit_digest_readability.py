#!/usr/bin/env python3
"""Abdelhamid ticket 2026-07-03 — digest readability.

Verifies every acceptance criterion he listed:

  1. No raw English action code (STATUS_CHANGED, ASSIGNEES_CHANGED,
     CREATED, COMMENT_ADDED) leaks into the rendered body.
  2. Every task/lead is referenced by its actual title/name, not #id.
  3. No dashed placeholder line for empty subject/body.
  4. Tasks section explicitly split into "خلّصها" + "لسه شغال عليها".
  5. Events within each section rendered chronologically.
  6. Event count in the rendered body === count of raw source rows.
     (No data hidden by the formatting.)
  7. Section header carries a summary line (e.g. "N خلصوا، M شغالين"
     for tasks; type breakdown for lead activities).
"""
import sys
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__DIGEST_READ_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company
    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown_company(existing.id)
    c = Company(name=COMPANY_NAME, base_currency="SAR")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)
    _STATE["company_id"] = c.id
    db.session.commit()


def _teardown_company(company_id):
    from sqlalchemy import text, inspect
    db.session.close()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id = :c"
                ), {"c": company_id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                       {"c": company_id})


def _make_fixture():
    """Build a scenario mirroring what Abdelhamid saw in the screenshot:
    2 closed tasks + 2 open tasks + 4 lead activities (mix of subjects
    empty/full) + 2 lead-status events."""
    from app.models import (
        User, UserStatus, Task, TaskStatus, TaskPriority,
        Lead, LeadStatus, LeadType, LeadSource,
    )
    from app.models.crm import LeadStatusEvent, TaskActivityLog
    from app.models.crm_expansion import LeadActivity, LeadActivityType
    from app.models.user import user_companies

    cid = _STATE["company_id"]
    now = datetime.utcnow()

    # Actor user — the "employee" whose digest we're building.
    u = User(email="digest_actor@x.co", full_name="أسماء عاطف",
              status=UserStatus.ACTIVE.value)
    u.set_password("x")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=cid, role="sales_rep",
    ))

    # ─── Tasks: 2 that end DONE + 2 that stay open ────────────────
    tasks = []
    for title in ("تجهيز عرض السعر لشركة النور",
                    "متابعة العميل زكريا",
                    "عمل مقابلة كاندبديت",
                    "مراجعة الفاتورة رقم 200"):
        t = Task(company_id=cid, title=title,
                   status=TaskStatus.IN_PROGRESS,
                   priority=TaskPriority.MEDIUM,
                   assigned_to_id=u.id,
                   created_by_id=u.id)
        db.session.add(t); db.session.flush()
        tasks.append(t)
    db.session.commit()

    # Task 0 & 1 — get closed (STATUS_CHANGED -> DONE at end of day).
    # Task 2 & 3 — comment/assignee events only, still open.
    events = [
        (tasks[0], "CREATED",           None,                {"status": "IN_PROGRESS"}),
        (tasks[0], "COMMENT_ADDED",     None,                None),
        (tasks[0], "STATUS_CHANGED",    {"status": "IN_PROGRESS"}, {"status": "DONE"}),
        (tasks[1], "STATUS_CHANGED",    {"status": "IN_PROGRESS"}, {"status": "DONE"}),
        (tasks[2], "ASSIGNEES_CHANGED", {"assignees": []},         {"assignees": [u.id]}),
        (tasks[2], "CREATED",           None,                None),
        (tasks[3], "COMMENT_ADDED",     None,                None),
    ]
    for i, (t, action, before, after) in enumerate(events):
        db.session.add(TaskActivityLog(
            company_id=cid, task_id=t.id, user_id=u.id, action=action,
            before_json=json.dumps(before) if before else None,
            after_json=json.dumps(after) if after else None,
            created_at=now - timedelta(hours=len(events) - i),
        ))

    # ─── Leads + status events + activities ───────────────────────
    leads = []
    for name in ("محمد بشير", "زكريا الفار", "شركة النور", "أحمد العابد"):
        L = Lead(company_id=cid, client_name=name,
                    phone="0500000000",
                    service_needed="خدمة اختبار",
                    status=LeadStatus.NEW_LEAD,
                    lead_type=LeadType.INBOUND.value,
                    source=LeadSource.WEBSITE.value,
                    created_by_id=u.id, assigned_to_id=u.id)
        db.session.add(L); db.session.flush()
        leads.append(L)
    db.session.commit()

    # 2 stage transitions.
    db.session.add(LeadStatusEvent(
        lead_id=leads[0].id, from_status=LeadStatus.NEW_LEAD,
        to_status=LeadStatus.CONTACTED, changed_by_id=u.id,
        created_at=now - timedelta(hours=5),
    ))
    db.session.add(LeadStatusEvent(
        lead_id=leads[2].id, from_status=LeadStatus.CONTACTED,
        to_status=LeadStatus.MEETING_SCHEDULED, changed_by_id=u.id,
        created_at=now - timedelta(hours=2),
    ))

    # 4 activities — 2 with subject, 2 without (§3 test).
    acts = [
        (leads[0], LeadActivityType.CALL,    "",                        now - timedelta(hours=6)),
        (leads[1], LeadActivityType.MEETING, "الساعة 3 في المكتب",     now - timedelta(hours=4)),
        (leads[2], LeadActivityType.NOTE,    "",                        now - timedelta(hours=3)),
        (leads[3], LeadActivityType.EMAIL,   "أرسلت العرض المالي",     now - timedelta(hours=1)),
    ]
    for lead, atype, subj, ts in acts:
        db.session.add(LeadActivity(
            company_id=cid, lead_id=lead.id, type=atype,
            subject=subj or None,
            activity_date=ts, created_by_id=u.id,
            created_at=ts,
        ))

    db.session.commit()
    _STATE.update(
        user_id=u.id,
        task_ids=[t.id for t in tasks],
        lead_ids=[L.id for L in leads],
        total_events=(
            len(events)   # task logs
            + 2           # lead-status events
            + len(acts)   # lead activities
        ),
    )


def _render():
    """Call the same _summarise() the production digest uses."""
    from app.services.daily_digest import (
        _summarise,
        _fetch_task_activity, _fetch_lead_status_events,
        _fetch_lead_activities, _fetch_user_activity,
    )
    cid = _STATE["company_id"]
    uid = _STATE["user_id"]
    day = date.today()
    user_logs = _fetch_user_activity(cid, uid, day)
    task_logs = _fetch_task_activity(cid, uid, day)
    lead_events = _fetch_lead_status_events(uid, day)
    lead_acts = _fetch_lead_activities(cid, uid, day)
    body = _summarise(user_logs, task_logs, lead_events, lead_acts)
    _STATE["body"] = body
    _STATE["task_logs"] = task_logs
    _STATE["lead_events"] = lead_events
    _STATE["lead_acts"] = lead_acts
    return body


@check("Setup: fixture built")
def _():
    _make_fixture()
    body = _render()
    assert body.strip(), "empty body"
    return f"body={len(body)} chars"


@check("§1 no raw English action codes leak into the body")
def _():
    body = _STATE["body"]
    forbidden = ("STATUS_CHANGED", "COMMENT_ADDED", "ASSIGNEES_CHANGED",
                  "CREATED", "NEW_LEAD", "CONTACTED", "MEETING_SCHEDULED",
                  "IN_PROGRESS", "DONE")
    hits = [w for w in forbidden if w in body]
    assert not hits, f"raw code(s) leaked: {hits}"
    return "no raw codes present"


@check("§2 tasks/leads are shown by name not #id")
def _():
    body = _STATE["body"]
    assert "تجهيز عرض السعر" in body, "task 0 title missing"
    assert "محمد بشير" in body, "lead 0 name missing"
    # No '#<digit>' patterns should appear (some templates use # for
    # references like INV-#123 — allow those). We check specifically
    # that "مهمة #<n>" and "ليد #<n>" patterns are gone.
    import re
    assert not re.search(r"مهمة\s+#\d", body), "raw مهمة #id leaked"
    assert not re.search(r"ليد\s+#\d", body), "raw ليد #id leaked"
    return "names present, ids scrubbed"


@check("§3 empty subject renders clean, no dash placeholder")
def _():
    body = _STATE["body"]
    # A blank-subject CALL should now read "📞 مكالمة مع محمد بشير".
    # It must NOT read "مكالمة: —".
    assert "📞 مكالمة مع محمد بشير" in body, \
        f"empty-subject call not rendered clean: {body!r}"
    assert "مكالمة: —" not in body, "dash line leaked"
    assert "ملاحظة: —" not in body, "dash line leaked"
    return "empty subject renders as 'نوع مع اسم'"


@check("§4 chronological order within each section")
def _():
    body = _STATE["body"]
    # Lead activities were injected in order call → meeting → note → email.
    # After sorting by created_at ascending, the same order should hold.
    call_at = body.find("مكالمة مع محمد بشير")
    meeting_at = body.find("اجتماع: الساعة 3 في المكتب")
    note_at = body.find("ملاحظة مع شركة النور")
    email_at = body.find("إيميل: أرسلت العرض المالي")
    positions = [call_at, meeting_at, note_at, email_at]
    assert -1 not in positions, f"missing line, positions={positions}"
    assert positions == sorted(positions), \
        f"lead activities not chronological: {positions}"
    return "lead-activity section chronological"


@check("§5 tasks split into 'خلّصها' and 'لسه شغال عليها'")
def _():
    body = _STATE["body"]
    assert "خلّصها" in body, "closed sub-header missing"
    assert "لسه شغال عليها" in body, "open sub-header missing"
    # Tasks 0 & 1 were closed; 2 & 3 open. Verify grouping.
    closed_idx = body.find("خلّصها")
    open_idx = body.find("لسه شغال عليها")
    assert closed_idx < open_idx, "closed section should appear before open"
    closed_section = body[closed_idx:open_idx]
    open_section = body[open_idx:]
    assert "تجهيز عرض السعر" in closed_section, "closed task 0 not in closed bucket"
    assert "متابعة العميل زكريا" in closed_section, "closed task 1 not in closed bucket"
    assert "عمل مقابلة" in open_section, "open task 2 not in open bucket"
    assert "مراجعة الفاتورة" in open_section, "open task 3 not in open bucket"
    return "2 خلصوا + 2 لسه شغالين separated correctly"


@check("§6 LeadActivityType icons rendered")
def _():
    body = _STATE["body"]
    for icon in ("📞", "🤝", "📝", "✉️"):
        assert icon in body, f"icon {icon} missing"
    return "call/meeting/note/email icons all present"


@check("§7 section summary line at the top of each section")
def _():
    body = _STATE["body"]
    # Task header should include closed/open counts.
    import re
    m = re.search(r"\*\*المهام\*\* \(\d+\) — \d+ خلصوا، \d+ لسه شغالين", body)
    assert m, "task header missing done/open summary"
    # Lead-activities header should include type breakdown w/ icons.
    m2 = re.search(r"\*\*متابعات العملاء المحتملين\*\* \(\d+\) — .+×", body)
    assert m2, "activities header missing type breakdown"
    return f"headers: {m.group(0)!r}"


@check("Data invariant: rendered event count === raw event count")
def _():
    body = _STATE["body"]
    total = _STATE["total_events"]
    # Every raw event should appear on some bullet line. We count
    # bullet lines that start with '  •' or '    •' (nested).
    bullet_lines = [
        line for line in body.splitlines()
        if line.lstrip().startswith("•")
    ]
    # Each task_log contributes an *action* on a task line. Multiple
    # actions get joined with '،' into one bullet, so we count actions
    # by summing the segments across task bullets in خلّصها + لسه شغال.
    # The invariant we need is: no event silently vanished.
    action_words = 0
    for line in bullet_lines:
        # Only task bullets under خلّصها/لسه شغال are aggregated with '،'.
        if "**" in line and "—" in line:
            # A task bullet: "**title** — action, action"
            _, rhs = line.split("—", 1)
            action_words += len([w for w in rhs.split("،") if w.strip()])
        else:
            action_words += 1
    assert action_words == total, \
        f"expected {total} events rendered, got {action_words}"
    return f"{total} raw events → {action_words} rendered lines"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        from tests._orphan_sweep import preflight
        preflight()
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
                    _teardown_company(_STATE["company_id"])
                    print(f"\n(cleaned up fixture company)")
                # Also drop the actor user.
                from sqlalchemy import text
                with db.engine.begin() as conn:
                    conn.execute(text(
                        "DELETE FROM user_companies WHERE user_id IN "
                        "(SELECT id FROM users WHERE email = 'digest_actor@x.co')"
                    ))
                    conn.execute(text(
                        "DELETE FROM users WHERE email = 'digest_actor@x.co'"
                    ))
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
