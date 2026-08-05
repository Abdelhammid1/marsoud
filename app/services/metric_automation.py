"""MARSOUD-METRIC-AUTOMATION (2026-08-05) — activity log → metric entries.

Recording performance metrics was entirely manual, and so were opening
the monthly evaluation cycle and setting each employee's targets. Three
repetitive jobs every month, each of which is only ever a transcription
of something the system already knows.

THE ARCHITECTURAL DECISION, from the ticket: `UserActivityLog` is the
ONE source. This module never reads the Lead or Task tables. That is why
`change_lead_status` and `set_task_status` put the from/to statuses into
the log row's `extra_data` — an event has to be scorable from the row
alone, or the score would describe what the lead looks like TODAY rather
than what happened at the time.

Three jobs live here:

  award_metric_entries()   activity rows  → MetricLogEntry
  open_monthly_cycles()    first of month → EvaluationCycle
  seed_targets_for_cycle() cycle opened   → EmployeeTarget per employee

All three are idempotent, because cron double-fires and a retry must
never double-score.
"""
import json
import logging
from calendar import monthrange
from datetime import date, datetime

from app import db

log = logging.getLogger("ledgeros.metrics")


# ─── The points table ───────────────────────────────────────────────────
# THE ONE PLACE these values live. Changing a number here changes what
# the job awards from the next tick; no other code knows them.
#
# Keyed by (entity_type, event) where `event` is the target status for a
# transition, or a synthetic name for a creation.
#
# ZERO MEANS THE RULE IS REGISTERED BUT AWARDS NOTHING. The four below
# are «تحدد لاحقًا» in the ticket — the pipeline is built and tested for
# them, and each goes live the moment a number replaces the 0. Nothing
# else needs to change.
POINTS = {
    # ── Leads: values given in the ticket ──────────────────────────────
    ("lead", "CONTACTED"):          5,
    ("lead", "MEETING_SCHEDULED"): 10,
    ("lead", "WON"):               15,

    # ── تحدد لاحقًا — set a value and the rule starts scoring ──────────
    ("task", "DONE_ON_TIME"):       0,
    ("invoice", "POSTED"):          0,
    ("vendor_bill", "POSTED"):      0,
    ("customer", "CREATED"):        0,
}

# Which metric key each rule writes against. The target must exist for
# the employee in the open cycle or log_metric_entry refuses the entry —
# seed_targets_for_cycle is what guarantees it does.
METRIC_KEYS = {
    "lead":        "lead_progress",
    "task":        "tasks_done",
    "invoice":     "invoices_posted",
    "vendor_bill": "bills_posted",
    "customer":    "customers_added",
}

# Metric keys seeded for every employee when a cycle opens, with the
# category each belongs to. target_value 0 means "no quota set" — the
# target exists so entries are legal; management can set real numbers
# from the targets page whenever they want.
SEEDED_TARGETS = (
    ("lead_progress",   "TARGET_ACHIEVEMENT"),
    ("tasks_done",      "EXECUTION_QUALITY"),
    ("invoices_posted", "TARGET_ACHIEVEMENT"),
    ("bills_posted",    "EXECUTION_QUALITY"),
    ("customers_added", "GROWTH"),
)

# Lead stages in ascending order of value. The ticket: the same employee
# on the same lead scores their BEST stage, not the sum of the stages
# they walked it through.
LEAD_STAGE_ORDER = ("CONTACTED", "MEETING_SCHEDULED", "WON")


class MetricAutomationError(Exception):
    pass


# ─── Reading an activity row ────────────────────────────────────────────
def _extra(row):
    if not row.extra_data:
        return {}
    try:
        data = json.loads(row.extra_data)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _event_for(row):
    """(event_name, extra) for a log row, or (None, extra) if it scores
    nothing. Everything comes off the row — never the source table."""
    extra = _extra(row)
    etype = (row.entity_type or "").lower()

    if etype == "lead":
        return extra.get("to_status"), extra
    if etype == "task":
        if extra.get("to_status") != "DONE":
            return None, extra
        # A task closed LATE scores nothing; the ticket's rule is
        # "إغلاق (DONE) في الميعاد".
        return ("DONE_ON_TIME" if extra.get("on_time") else None), extra
    if etype in ("invoice", "vendor_bill"):
        return ("POSTED" if row.action_type in ("CREATE", "APPROVE")
                else None), extra
    if etype == "customer":
        return ("CREATED" if row.action_type == "CREATE" else None), extra
    return None, extra


def _recipients(row, extra):
    """Whose score this is. A list, because a task splits across everyone
    who was on it — and it is the ACTOR for everything else, which is the
    ticket's «قفل الصفقة يُحتسب لصاحب الفعل الفعلي»."""
    if (row.entity_type or "").lower() == "task":
        ids = extra.get("assignee_ids") or []
        ids = [i for i in ids if i]
        if ids:
            return ids
    return [row.user_id] if row.user_id else []


def _employee_for(user_id, company_id):
    from app.models import Employee
    if not user_id:
        return None
    return Employee.query.filter_by(
        user_id=user_id, company_id=company_id).first()


# ─── Job 1: award points ────────────────────────────────────────────────
def award_metric_entries(company_id=None, now=None):
    """Turn new activity rows into MetricLogEntry rows.

    Returns a summary of what was created and why anything was skipped —
    a job that silently does nothing looks identical to a job that is
    working, which is how a broken cron survives for months.
    """
    from app.models import (UserActivityLog, MetricLogEntry, EvaluationCycle,
                            EvaluationCycleStatus, Company)
    from app.services.evaluation import log_metric_entry, EvaluationError

    today = now or date.today()
    summary = {"created": 0, "skipped": {}, "companies": 0}

    def skip(reason):
        summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1

    companies = ([db.session.get(Company, company_id)] if company_id
                 else Company.query.filter_by(is_active=True).all())

    for co in [c for c in companies if c is not None]:
        cycle = (EvaluationCycle.query
                 .filter_by(company_id=co.id,
                            status=EvaluationCycleStatus.OPEN.value)
                 .filter(EvaluationCycle.start_date <= today)
                 .filter(EvaluationCycle.end_date >= today)
                 .order_by(EvaluationCycle.start_date.desc()).first())
        if cycle is None:
            # The ticket is explicit: no OPEN cycle, no entries. Not an
            # error — most companies will sit like this between cycles.
            skip("no open cycle")
            continue
        summary["companies"] += 1

        # Only events inside the cycle's window, and only ones not
        # already scored in THIS cycle.
        scored = {r.source_activity_id for r in MetricLogEntry.query
                  .filter(MetricLogEntry.cycle_id == cycle.id,
                          MetricLogEntry.source_activity_id.isnot(None)).all()}

        rows = (UserActivityLog.query
                .filter(UserActivityLog.company_id == co.id,
                        UserActivityLog.entity_type.in_(list(METRIC_KEYS)))
                .filter(UserActivityLog.created_at
                        >= datetime.combine(cycle.start_date,
                                            datetime.min.time()))
                .order_by(UserActivityLog.id.asc()).all())

        # «أعلى مرحلة لكل Lead» — the best stage, and it has to hold
        # ACROSS ticks, not just within one batch. A lead contacted on
        # Monday (5) and won on Friday must total 15, not 5 + 15: the
        # Monday entry is already committed by the time Friday's tick
        # runs, so the new one can only be worth the DIFFERENCE.
        #
        # MetricLogEntry has no lead column, so what was already awarded
        # per lead is reconstructed through the source event —
        # source_activity_id → UserActivityLog.entity_id → the lead. That
        # is the same single-source rule this whole module follows.
        awarded_by_lead = {}
        if scored:
            prior = (db.session.query(MetricLogEntry, UserActivityLog)
                     .join(UserActivityLog,
                           MetricLogEntry.source_activity_id
                           == UserActivityLog.id)
                     .filter(MetricLogEntry.cycle_id == cycle.id,
                             UserActivityLog.entity_type == "lead").all())
            for entry, act in prior:
                k = (entry.employee_id, act.entity_id)
                awarded_by_lead[k] = round(
                    awarded_by_lead.get(k, 0.0) + float(entry.value or 0), 4)

        best_lead = {}

        for row in rows:
            if row.id in scored:
                skip("already scored")
                continue
            event, extra = _event_for(row)
            if event is None:
                skip("not a scoring event")
                continue
            points = POINTS.get((row.entity_type.lower(), event))
            if not points:
                skip("rule scores zero" if points == 0 else "no rule")
                continue
            people = _recipients(row, extra)
            if not people:
                skip("no user on the event")
                continue

            share = round(points / len(people), 4)
            for uid in people:
                emp = _employee_for(uid, co.id)
                if emp is None:
                    skip("actor is not an employee")
                    continue
                if row.entity_type.lower() == "lead":
                    key = (emp.id, row.entity_id)
                    prev = best_lead.get(key)
                    rank = LEAD_STAGE_ORDER.index(event) \
                        if event in LEAD_STAGE_ORDER else -1
                    if prev is None or rank > prev[0]:
                        best_lead[key] = (rank, share, row.id, row.created_at)
                    continue
                if _write(co, cycle, emp, row, share, summary, skip):
                    pass

        for (emp_id, lead_id), (_rank, share, act_id, _when) in best_lead.items():
            from app.models import Employee
            already = awarded_by_lead.get((emp_id, lead_id), 0.0)
            delta = round(share - already, 4)
            if delta <= 0:
                # This event is worth no more than what the lead has
                # already earned — an earlier stage arriving late, or a
                # stage that was superseded before it was ever scored.
                skip("lead already scored at this stage or higher")
                continue
            emp = db.session.get(Employee, emp_id)
            src = db.session.get(UserActivityLog, act_id)
            if emp and src:
                _write(co, cycle, emp, src, delta, summary, skip)

    return summary


def _ensure_target(cycle, employee_id, metric_key):
    """Make sure the employee has a target for this metric in this cycle.

    Found by auditing: targets are seeded when a cycle OPENS, so anyone
    hired afterwards had none — and log_metric_entry refuses an entry
    without one. A mid-cycle joiner therefore scored NOTHING for their
    whole first month, silently, reported only as a skip reason nobody
    reads.

    Seeding on demand costs one query on the miss path and makes the
    join date irrelevant.
    """
    from app.models import EmployeeTarget
    from app.services.evaluation import upsert_target, EvaluationError
    exists = EmployeeTarget.query.filter_by(
        cycle_id=cycle.id, employee_id=employee_id,
        metric_key=metric_key).first()
    if exists:
        return True
    category = dict(SEEDED_TARGETS).get(metric_key)
    if not category:
        return False
    try:
        upsert_target(cycle=cycle, employee_id=employee_id,
                      metric_key=metric_key, target_value=0,
                      weight_pct=0, category=category)
        return True
    except EvaluationError:
        # A SUBMITTED/LOCKED cycle refuses new targets. Nothing to do —
        # log_metric_entry will refuse the entry too, with a reason.
        return False


def _write(company, cycle, employee, row, value, summary, skip):
    from app.services.evaluation import log_metric_entry, EvaluationError
    key = METRIC_KEYS.get((row.entity_type or "").lower())
    if not key:
        skip("no metric key")
        return False
    _ensure_target(cycle, employee.id, key)
    try:
        log_metric_entry(
            company_id=company.id, cycle=cycle, employee_id=employee.id,
            metric_key=key,
            entry_date=(row.created_at.date() if row.created_at
                        else date.today()),
            value=value, entered_by_id=None, source_activity_id=row.id,
        )
        summary["created"] += 1
        return True
    except EvaluationError as e:
        # The commonest cause is a missing target, which
        # seed_targets_for_cycle exists to prevent. Counted, not raised —
        # one bad employee must not stop the sweep.
        skip(f"refused: {str(e)[:40]}")
        return False
    except Exception:
        db.session.rollback()
        log.exception("metric entry failed for activity %s", row.id)
        skip("error")
        return False


# ─── Job 2: targets when a cycle opens ──────────────────────────────────
def seed_targets_for_cycle(cycle, employees=None):
    """One EmployeeTarget per active employee per seeded metric.

    Not a nicety: log_metric_entry REFUSES an entry with no matching
    target, so without this the awarding job records nothing at all.
    """
    from app.models import Employee, EmployeeTarget, EmployeeStatus
    from app.services.evaluation import upsert_target, EvaluationError

    if employees is None:
        employees = Employee.query.filter_by(
            company_id=cycle.company_id, status=EmployeeStatus.ACTIVE).all()

    created = 0
    for emp in employees:
        for key, category in SEEDED_TARGETS:
            exists = EmployeeTarget.query.filter_by(
                cycle_id=cycle.id, employee_id=emp.id, metric_key=key).first()
            if exists:
                continue
            try:
                # target_value 0 = no quota yet. The row exists so entries
                # are legal; management sets real numbers when they want.
                upsert_target(cycle=cycle, employee_id=emp.id,
                              metric_key=key, target_value=0,
                              weight_pct=0, category=category)
                created += 1
            except EvaluationError:
                log.exception("target seed refused for employee %s", emp.id)
    return created


# ─── Job 3: open the monthly cycle ──────────────────────────────────────
def open_monthly_cycles(now=None, company_id=None, force=False):
    """Open this month's cycle for every active company.

    Idempotent on (company, year, month): a cycle whose start_date falls
    in the month already exists → nothing happens, whatever its status.
    `force` only bypasses the first-of-month check, never that.
    """
    from app.models import Company, EvaluationCycle
    from app.services.evaluation import create_cycle, EvaluationError
    from sqlalchemy import extract

    today = now or date.today()
    summary = {"opened": 0, "targets": 0, "skipped": {}}

    def skip(reason):
        summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1

    if today.day != 1 and not force:
        summary["skipped"]["not the first of the month"] = 1
        return summary

    start = date(today.year, today.month, 1)
    end = date(today.year, today.month,
               monthrange(today.year, today.month)[1])

    companies = ([db.session.get(Company, company_id)] if company_id
                 else Company.query.filter_by(is_active=True).all())

    for co in [c for c in companies if c is not None]:
        existing = (EvaluationCycle.query
                    .filter(EvaluationCycle.company_id == co.id)
                    .filter(extract("year", EvaluationCycle.start_date)
                            == today.year)
                    .filter(extract("month", EvaluationCycle.start_date)
                            == today.month).first())
        if existing:
            skip("cycle already exists for this month")
            continue
        try:
            cycle = create_cycle(
                company_id=co.id,
                name=f"دورة {today.year}-{today.month:02d}",
                period_type="MONTHLY", start_date=start, end_date=end,
                created_by_id=None)
            summary["opened"] += 1
            summary["targets"] += seed_targets_for_cycle(cycle)
        except EvaluationError as e:
            skip(f"refused: {str(e)[:40]}")
        except Exception:
            db.session.rollback()
            log.exception("cycle open failed for company %s", co.id)
            skip("error")

    return summary


def open_cycle_now(company_id, *, name=None, start=None, created_by_id=None):
    """Open a cycle starting TODAY and ending at month end.

    The August 2026 exception: the ticket wants that one cycle dated from
    the day the work actually deploys, not from the 1st, and with no
    backfill of anything earlier. That date is not knowable from here, so
    it is a command run on deploy day rather than a constant in the code.
    """
    from app.models import EvaluationCycle
    from app.services.evaluation import create_cycle
    from sqlalchemy import extract

    start = start or date.today()
    end = date(start.year, start.month,
               monthrange(start.year, start.month)[1])
    existing = (EvaluationCycle.query
                .filter(EvaluationCycle.company_id == company_id)
                .filter(extract("year", EvaluationCycle.start_date)
                        == start.year)
                .filter(extract("month", EvaluationCycle.start_date)
                        == start.month).first())
    if existing:
        return existing, 0, False
    cycle = create_cycle(
        company_id=company_id,
        name=name or f"دورة {start.year}-{start.month:02d}",
        period_type="MONTHLY", start_date=start, end_date=end,
        created_by_id=created_by_id)
    return cycle, seed_targets_for_cycle(cycle), True
