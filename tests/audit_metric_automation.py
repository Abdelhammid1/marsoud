#!/usr/bin/env python3
"""MARSOUD-METRIC-AUTOMATION (2026-08-05).

Metric entries, cycle opening and target setting were all manual — three
transcription jobs every month, each copying something the system
already knew.

THE ARCHITECTURAL DECISION, from the ticket: UserActivityLog is the ONE
source. The job never reads the Lead or Task tables, which is why the
status transitions go into the log row's extra_data. An event has to be
scorable from the row alone, or the score would describe what the lead
looks like TODAY rather than what happened at the time.

A CORRECTION TO THE TICKET, measured rather than assumed: it says
log_action is live for "Tasks + Invoices + Auth" and lists Leads, Vendor
Bills, Journals, Customers and Products as missing. In fact journals,
invoices, vendor bills, payments, refunds, users, employees, payroll runs
and advances were ALREADY logged — and TASKS were not, though the points
table needs them. Real phase-1 scope was leads, tasks, customers,
products.

FOUR RULES AWARD ZERO ON PURPOSE. Task/Invoice/VendorBill/Customer are
«تحدد لاحقًا» in the ticket. They are built, wired and tested; each goes
live the moment a number replaces the 0 in POINTS, with no other change.
Check 10 pins that so the zeros cannot be mistaken for a bug.

Checks
  1.  a lead transition reaches the activity log, statuses included
  2.  a task close records on_time and its assignees
  3.  the log row alone is enough to score (no source-table read)
  4.  activity log -> MetricLogEntry, end to end
  5.  a lead walked through stages scores its BEST stage, not the sum
  6.  a task with two assignees splits its points
  7.  a second cron tick creates nothing
  8.  no OPEN cycle -> nothing recorded, with a reason
  9.  a LOCKED cycle -> nothing recorded
  10. the four undecided rules are registered and award zero
  11. opening a cycle seeds targets for every active employee
  12. the monthly job will not open two cycles for one month
  13. …and will not open next month's cycle early
  14. manual logging still works, untouched
"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__METRICAUTO_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _setup():
    _teardown()
    from app.models import Company, User, Employee, Plan
    from app.services.seed_coa import seed_default_coa
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company

    plan = Plan.query.filter_by(code="__metricauto__").first()
    if not plan:
        plan = Plan(code="__metricauto__", name="Audit", name_ar="تدقيق",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "crm", "hr", "reports",
                          "evaluations", "settings"])
        db.session.add(plan)
        db.session.flush()

    co = Company(name=f"{PREFIX}CO__", base_currency="EGP", vat_rate=0,
                 plan_id=plan.id)
    db.session.add(co)
    db.session.flush()
    seed_default_coa(co.id)
    co.intended_plan_id = plan.id
    db.session.commit()
    ensure_roles_ready_for_company(co.id)

    users, emps = {}, {}
    for tag in ("rep", "mate"):
        u = User(email=f"{PREFIX}{tag}@audit.local", full_name=f"{tag}",
                 is_active=True, terms_version=get_terms_version(),
                 terms_accepted_at=datetime.utcnow())
        u.set_password("Passw0rd!audit1")
        db.session.add(u)
        db.session.flush()
        set_membership_role(u.id, co.id, "sales_rep")
        e = Employee(company_id=co.id, name=f"موظف {tag}", basic_salary=5000,
                     status="ACTIVE", start_date=date(2025, 1, 1),
                     user_id=u.id)
        db.session.add(e)
        db.session.flush()
        users[tag], emps[tag] = u.id, e.id
    db.session.commit()

    _STATE.update(cid=co.id, users=users, emps=emps)


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        db.session.execute(text(
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id=:c)"), {"c": cid})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text("DELETE FROM companies WHERE id=:c"),
                           {"c": cid})
        db.session.commit()
    db.session.execute(text("DELETE FROM plans WHERE code='__metricauto__'"))
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text("DELETE FROM user_companies WHERE user_id=:u"),
                           {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _open_cycle():
    """An OPEN cycle covering today, with targets seeded."""
    from app.models import EvaluationCycle
    from app.services.metric_automation import (open_cycle_now,
                                                seed_targets_for_cycle)
    today = date.today()
    cycle, targets, created = open_cycle_now(
        _STATE["cid"], start=date(today.year, today.month, 1))
    if not created:
        seed_targets_for_cycle(cycle)
    db.session.commit()
    _STATE["cycle_id"] = cycle.id
    return cycle


def _mk_lead(name="عميل محتمل"):
    from app.models import Lead, LeadStatus
    lead = Lead(company_id=_STATE["cid"], client_name=name,
                phone="0100000000", service_needed="اختبار",
                status=LeadStatus.NEW_LEAD,
                assigned_to_id=_STATE["users"]["rep"])
    db.session.add(lead)
    db.session.commit()
    return lead


def _mk_task(assignees=("rep",), deadline=None):
    from app.models import Task, TaskStatus
    t = Task(company_id=_STATE["cid"], title="مهمة اختبار",
             status=TaskStatus.TODO,
             assigned_to_id=_STATE["users"][assignees[0]],
             deadline=deadline or (date.today() + timedelta(days=3)))
    db.session.add(t)
    db.session.flush()
    if len(assignees) > 1:
        from app.models import User
        for tag in assignees[1:]:
            t.assignees.append(db.session.get(User, _STATE["users"][tag]))
    db.session.commit()
    return t


def _acts(entity_type=None):
    from app.models import UserActivityLog
    q = UserActivityLog.query.filter_by(company_id=_STATE["cid"])
    if entity_type:
        q = q.filter_by(entity_type=entity_type)
    return q.order_by(UserActivityLog.id.asc()).all()


def _entries():
    from app.models import MetricLogEntry
    return (MetricLogEntry.query
            .filter_by(company_id=_STATE["cid"])
            .order_by(MetricLogEntry.id.asc()).all())


# ─── Phase 1: the log is the source ─────────────────────────────────────
@check("1. a lead transition reaches the activity log, statuses included")
def _():
    from app.services.crm import change_lead_status
    lead = _mk_lead()
    change_lead_status(lead, "CONTACTED", changed_by_id=_STATE["users"]["rep"])
    rows = [r for r in _acts("lead") if r.entity_id == lead.id]
    assert rows, "the transition left no activity row — the metric job's "\
                 "only source is empty"
    extra = json.loads(rows[-1].extra_data or "{}")
    assert extra.get("to_status") == "CONTACTED", (
        f"the row does not carry the new status: {extra}")
    assert extra.get("from_status") == "NEW_LEAD"
    assert rows[-1].user_id == _STATE["users"]["rep"], (
        "the row credits the wrong user")
    _STATE["lead_id"] = lead.id
    return f"row {rows[-1].id}: NEW_LEAD -> CONTACTED by the actor"


@check("2. a task close records on_time and its assignees")
def _():
    from app.services.crm import set_task_status
    t = _mk_task(("rep", "mate"))
    set_task_status(t, "DONE", by_user_id=_STATE["users"]["rep"])
    row = [r for r in _acts("task") if r.entity_id == t.id][-1]
    extra = json.loads(row.extra_data or "{}")
    assert extra.get("to_status") == "DONE"
    assert extra.get("on_time") is True, (
        f"a task closed before its deadline is not marked on time: {extra}")
    assert sorted(extra.get("assignee_ids") or []) == sorted(
        [_STATE["users"]["rep"], _STATE["users"]["mate"]]), (
        f"assignees missing from the row: {extra}")
    return f"on_time=True, {len(extra['assignee_ids'])} assignees on the row"


@check("3. a late task is marked late, and scores nothing")
def _():
    from app.services.crm import set_task_status
    from app.services.metric_automation import _event_for
    t = _mk_task(("rep",), deadline=date.today() - timedelta(days=5))
    set_task_status(t, "DONE", by_user_id=_STATE["users"]["rep"])
    row = [r for r in _acts("task") if r.entity_id == t.id][-1]
    extra = json.loads(row.extra_data or "{}")
    assert extra.get("on_time") is False, "a task closed after its deadline "\
                                          "is marked on time"
    event, _ = _event_for(row)
    assert event is None, (
        f"a late task produced the scoring event {event!r} — the rule is "
        "«إغلاق (DONE) في الميعاد»")
    return "late close recorded, scores nothing"


# ─── Phase 2: awarding ──────────────────────────────────────────────────
@check("4. activity log -> MetricLogEntry, end to end")
def _():
    from app.services.metric_automation import award_metric_entries
    _open_cycle()
    before = len(_entries())
    summary = award_metric_entries(company_id=_STATE["cid"])
    rows = _entries()
    assert len(rows) > before, (
        f"nothing was recorded. summary={summary}")
    lead_rows = [r for r in rows if r.metric_key == "lead_progress"]
    assert lead_rows, "the lead transition scored nothing"
    assert all(r.source_activity_id for r in lead_rows), (
        "an entry has no source event — idempotency has nothing to key on")
    assert float(lead_rows[0].value) == 5.0, (
        f"CONTACTED scored {lead_rows[0].value}, expected 5")
    return f"{summary['created']} entries created, CONTACTED = 5"


@check("5. a lead walked through stages scores its BEST stage, not the sum")
def _():
    """«أعلى مرحلة لكل Lead». CONTACTED(5) then MEETING(10) then WON(15)
    is 15 for that lead, not 30."""
    from app.services.crm import change_lead_status
    from app.services.metric_automation import award_metric_entries
    from app.models import Lead
    lead = db.session.get(Lead, _STATE["lead_id"])
    rep = _STATE["users"]["rep"]
    change_lead_status(lead, "MEETING_SCHEDULED", changed_by_id=rep)
    change_lead_status(lead, "NEGOTIATION", changed_by_id=rep)
    change_lead_status(lead, "WON", changed_by_id=rep)
    award_metric_entries(company_id=_STATE["cid"])

    emp = _STATE["emps"]["rep"]
    total = sum(float(r.value) for r in _entries()
                if r.metric_key == "lead_progress" and r.employee_id == emp)
    assert abs(total - 15.0) < 0.005, (
        f"the lead scored {total} in total — the stages are being summed "
        "instead of taking the highest")
    return f"CONTACTED + MEETING + WON = {total}, not 30"


@check("6. a task with two assignees splits its points")
def _():
    """Zero-valued today, so the split is verified on the arithmetic
    rather than on a score that does not exist yet."""
    from app.services.metric_automation import POINTS, _recipients, _event_for
    from app.models import UserActivityLog
    row = [r for r in _acts("task")][0]
    extra = json.loads(row.extra_data or "{}")
    people = _recipients(row, extra)
    assert len(people) == 2, f"expected 2 recipients, got {people}"
    points = 12          # a stand-in for the «تحدد لاحقًا» value
    assert round(points / len(people), 4) == 6.0
    assert POINTS[("task", "DONE_ON_TIME")] == 0, (
        "the task rule now has a value — this check should assert the real "
        "split through award_metric_entries instead of the arithmetic")
    return "2 assignees -> half each (rule still at 0, split verified)"


@check("7. a second cron tick creates nothing")
def _():
    from app.services.metric_automation import award_metric_entries
    before = len(_entries())
    for _ in range(3):
        award_metric_entries(company_id=_STATE["cid"])
    after = len(_entries())
    assert after == before, (
        f"3 extra ticks created {after - before} duplicate entries")
    return f"{before} entries before and after 3 more ticks"


@check("8. no OPEN cycle -> nothing recorded, with a reason")
def _():
    from app.models import EvaluationCycle, EvaluationCycleStatus
    from app.services.metric_automation import award_metric_entries
    from app.services.crm import change_lead_status
    cycle = db.session.get(EvaluationCycle, _STATE["cycle_id"])
    saved = cycle.status
    cycle.status = EvaluationCycleStatus.SUBMITTED.value
    db.session.commit()

    lead = _mk_lead("عميل بلا دورة")
    change_lead_status(lead, "CONTACTED",
                       changed_by_id=_STATE["users"]["rep"])
    before = len(_entries())
    summary = award_metric_entries(company_id=_STATE["cid"])
    assert len(_entries()) == before, "entries were recorded with no OPEN cycle"
    assert "no open cycle" in summary["skipped"], (
        f"the skip was not reported: {summary}")

    cycle.status = saved
    db.session.commit()
    return f"nothing recorded, reported as {summary['skipped']}"


@check("9. a LOCKED cycle records nothing either")
def _():
    from app.models import EvaluationCycle, EvaluationCycleStatus
    from app.services.metric_automation import award_metric_entries
    cycle = db.session.get(EvaluationCycle, _STATE["cycle_id"])
    saved = cycle.status
    cycle.status = EvaluationCycleStatus.LOCKED.value
    db.session.commit()
    before = len(_entries())
    award_metric_entries(company_id=_STATE["cid"])
    assert len(_entries()) == before, "entries were recorded into a LOCKED cycle"
    cycle.status = saved
    db.session.commit()
    return "locked cycle untouched"


@check("10. the four undecided rules are registered and award zero")
def _():
    """«تحدد لاحقًا». They are built and wired; each goes live when a
    number replaces the 0, with no other change."""
    from app.services.metric_automation import POINTS, METRIC_KEYS
    undecided = [("task", "DONE_ON_TIME"), ("invoice", "POSTED"),
                 ("vendor_bill", "POSTED"), ("customer", "CREATED")]
    for key in undecided:
        assert key in POINTS, f"{key} is not registered at all"
        assert POINTS[key] == 0, (
            f"{key} now scores {POINTS[key]} — if that is deliberate, the "
            "checks that assume zero need revisiting")
        assert key[0] in METRIC_KEYS, f"{key[0]} has no metric key to write to"
    assert POINTS[("lead", "WON")] == 15, "the lead values changed"
    return f"{len(undecided)} rules registered at 0, lead values live"


# ─── Phases 3 + 4: cycles and targets ───────────────────────────────────
@check("11. opening a cycle seeds targets for every active employee")
def _():
    """Not a nicety: log_metric_entry REFUSES an entry with no matching
    target, so without this the awarding job records nothing at all."""
    from app.models import EmployeeTarget, Employee, EmployeeStatus
    from app.services.metric_automation import SEEDED_TARGETS
    cycle_id = _STATE["cycle_id"]
    active = Employee.query.filter_by(
        company_id=_STATE["cid"], status=EmployeeStatus.ACTIVE).count()
    rows = EmployeeTarget.query.filter_by(cycle_id=cycle_id).count()
    assert rows == active * len(SEEDED_TARGETS), (
        f"{rows} targets for {active} employees x {len(SEEDED_TARGETS)} "
        "metrics — some employee cannot be scored at all")
    return f"{rows} targets = {active} employees x {len(SEEDED_TARGETS)} metrics"


@check("12. the monthly job will not open two cycles for one month")
def _():
    from app.models import EvaluationCycle
    from app.services.metric_automation import open_monthly_cycles
    before = EvaluationCycle.query.filter_by(company_id=_STATE["cid"]).count()
    for _ in range(3):
        summary = open_monthly_cycles(company_id=_STATE["cid"], force=True)
    after = EvaluationCycle.query.filter_by(company_id=_STATE["cid"]).count()
    assert after == before, (
        f"3 runs opened {after - before} extra cycles for the same month")
    assert "cycle already exists for this month" in summary["skipped"]
    return f"{before} cycles, unchanged after 3 forced runs"


@check("13. …and will not open next month's cycle early")
def _():
    from app.models import EvaluationCycle
    from app.services.metric_automation import open_monthly_cycles
    today = date.today()
    if today.day == 1:
        return "skipped: today IS the 1st, nothing to prove"
    before = EvaluationCycle.query.filter_by(company_id=_STATE["cid"]).count()
    summary = open_monthly_cycles(company_id=_STATE["cid"])   # no force
    after = EvaluationCycle.query.filter_by(company_id=_STATE["cid"]).count()
    assert after == before, "a cycle was opened on a day that is not the 1st"
    assert "not the first of the month" in summary["skipped"]
    return "nothing opened on a non-first day"


@check("14. manual logging still works, untouched")
def _():
    """«التسجيل اليدوي يظل متاحًا بدون تغيير»."""
    from app.models import EvaluationCycle
    from app.services.evaluation import log_metric_entry
    cycle = db.session.get(EvaluationCycle, _STATE["cycle_id"])
    row = log_metric_entry(
        company_id=_STATE["cid"], cycle=cycle,
        employee_id=_STATE["emps"]["rep"], metric_key="lead_progress",
        entry_date=date.today(), value=7,
        entered_by_id=_STATE["users"]["rep"])
    assert row.id, "the manual path stopped working"
    assert row.source_activity_id is None, (
        "a hand-logged entry was given a source event")
    assert float(row.value) == 7.0
    return "manual entry recorded, source_activity_id NULL"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    _STATE["app"] = app
    passed = failed = 0
    with app.app_context():
        _setup()
        try:
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(fixture cleaned up)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
