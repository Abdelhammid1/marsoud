"""MARSOUD-METRIC-AUTOMATION (2026-08-05) — open a cycle from today.

The ticket's one-off exception: August 2026's cycle is opened MANUALLY
and dated the day this work actually deploys, not the 1st of August, and
with no backfill of anything earlier. That date cannot be known while
writing the code, so it is a command run on deploy day.

    flask open-cycle-now                    # dry-run, all companies
    flask open-cycle-now --apply            # actually open
    flask open-cycle-now --company-id 8 --apply

From September onwards nothing here is needed: open_monthly_cycles runs
inside /cron/tick on the 1st of every month.

Idempotent — a company that already has a cycle starting in the current
month is skipped and reported, never given a second one.

ASCII-only output: the Windows console this gets run from is cp1252 and
an Arabic name in a print() aborts the whole command.
"""
import click
from flask.cli import with_appcontext

from app import db


def run(dry_run=True, company_id=None, name=None):
    """Returns a report dict. Writes only when dry_run is False."""
    from app.models import Company, Employee, EmployeeStatus
    from app.services.metric_automation import open_cycle_now, SEEDED_TARGETS
    from datetime import date

    today = date.today()
    report = {"dry_run": dry_run, "date": today.isoformat(),
              "opened": [], "skipped": [], "errors": []}

    companies = ([db.session.get(Company, company_id)] if company_id
                 else Company.query.filter_by(is_active=True).all())

    for co in [c for c in companies if c is not None]:
        emp_count = Employee.query.filter_by(
            company_id=co.id, status=EmployeeStatus.ACTIVE).count()
        if dry_run:
            from app.models import EvaluationCycle
            from sqlalchemy import extract
            existing = (EvaluationCycle.query
                        .filter(EvaluationCycle.company_id == co.id)
                        .filter(extract("year", EvaluationCycle.start_date)
                                == today.year)
                        .filter(extract("month", EvaluationCycle.start_date)
                                == today.month).first())
            if existing:
                report["skipped"].append(
                    (co.id, f"already has a cycle from "
                            f"{existing.start_date.isoformat()}"))
            else:
                report["opened"].append(
                    (co.id, "would open", emp_count * len(SEEDED_TARGETS)))
            continue

        try:
            cycle, targets, created = open_cycle_now(
                co.id, name=name, start=today)
            if created:
                db.session.commit()
                report["opened"].append((co.id, cycle.name, targets))
            else:
                report["skipped"].append(
                    (co.id, f"already has a cycle from "
                            f"{cycle.start_date.isoformat()}"))
        except Exception as e:                          # noqa: BLE001
            db.session.rollback()
            report["errors"].append((co.id, str(e)[:120]))

    return report


@click.command("open-cycle-now")
@click.option("--apply", "apply_", is_flag=True,
              help="Actually open the cycles. Default is a dry run.")
@click.option("--company-id", type=int, default=None,
              help="Limit to one company.")
@click.option("--name", default=None, help="Cycle name override.")
@with_appcontext
def backfill_cli(apply_, company_id, name):
    """Open an evaluation cycle starting TODAY (the August 2026 exception)."""
    report = run(dry_run=not apply_, company_id=company_id, name=name)

    print("")
    print("-" * 60)
    mode = "APPLY" if apply_ else "DRY-RUN"
    scope = f"company {company_id}" if company_id else "all active companies"
    print(f"{mode} - open evaluation cycle from {report['date']}, {scope}")
    print("-" * 60)
    for cid, label, targets in report["opened"]:
        print(f"  company {cid:4} : {label} ({targets} targets)")
    for cid, why in report["skipped"]:
        print(f"  company {cid:4} : SKIPPED - {why}")
    for cid, err in report["errors"]:
        print(f"  company {cid:4} : ERROR - {err}")
    print("-" * 60)
    print(f"opened  : {len(report['opened'])}")
    print(f"skipped : {len(report['skipped'])}")
    print(f"errors  : {len(report['errors'])}")
    if not apply_:
        print("")
        print("dry run - nothing written. re-run with --apply.")
    print("")
