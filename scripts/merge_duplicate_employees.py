"""Merge duplicate Employee rows within the same company that share an
email address (case-insensitive).

Root cause (Abdelhamid, 2026-07-02):
    Owner registration auto-creates an Employee for the owner. If the
    owner then goes to /payroll/employees/new and creates himself as
    an employee again, the payroll route (before this session's fix)
    happily wrote a SECOND Employee row — same email, same company.
    The owner then saw "two accounts" in the employee list.

This script finds those duplicates and merges them:
  primary = the earliest Employee row (lowest id) — assumed correct
  loser   = every later row with the same lowercase email

For each loser we reassign every FK we know about to primary, then
delete the loser. Any table that doesn't exist on the target DB is
skipped with a printed note (belt-and-braces for old DBs).

Usage:
    flask merge-duplicate-employees              # dry-run
    flask merge-duplicate-employees --apply      # actually merge

Registered in app/__init__.py near the other merge-* CLIs."""
from collections import defaultdict
import click
from flask.cli import with_appcontext
from sqlalchemy import func, inspect

from app import db
from app.models import Employee, User


# Every table that carries an FK to employees.id. Kept explicit so the
# merge is auditable at a glance.
_EMPLOYEE_FK_TABLES = [
    ("payroll_lines",         ["employee_id"]),
    ("employee_accruals",     ["employee_id"]),
    ("employee_history",      ["employee_id"]),
    ("leave_balances",        ["employee_id"]),
    ("leave_requests",        ["employee_id"]),
    ("attendance_exceptions", ["employee_id"]),
    ("sales_commissions",     ["sales_rep_id"]),
    ("employee_daily_reports", ["employee_id"]),
    ("employee_report_access", ["employee_id"]),
]


def _find_duplicate_groups():
    """Return [{company_id, email, employees: [Employee, ...]}, ...]
    for every (company, email) pair that has more than one Employee."""
    groups = defaultdict(list)
    for e in Employee.query.order_by(Employee.id).all():
        if not e.email:
            continue
        key = (e.company_id, e.email.strip().lower())
        groups[key].append(e)
    return [
        {"company_id": cid, "email": em, "employees": lst}
        for (cid, em), lst in groups.items() if len(lst) > 1
    ]


def _merge_pair(primary, loser):
    """Move every reference from loser → primary, then delete loser."""
    insp = inspect(db.engine)
    live_tables = set(insp.get_table_names())

    # 1. Reassign account_id link if the loser had a subsidiary account.
    #    Both are per-employee under 2130; if primary already has one,
    #    keep primary's; otherwise inherit the loser's.
    if loser.account_id and not primary.account_id:
        primary.account_id = loser.account_id
        loser.account_id = None
        db.session.flush()

    # 2. Reassign FKs on child tables.
    for table_name, cols in _EMPLOYEE_FK_TABLES:
        if table_name not in live_tables:
            print(f"    (skipped {table_name}: table not present)")
            continue
        for col in cols:
            try:
                db.session.execute(db.text(
                    f"UPDATE {table_name} SET {col} = :pid "
                    f"WHERE {col} = :lid"
                ), {"pid": primary.id, "lid": loser.id})
            except Exception as e:
                print(f"    (skipped {table_name}.{col}: {e})")

    # 3. Repoint any User whose employee_id was the loser's.
    for u in User.query.filter_by(employee_id=loser.id).all():
        u.employee_id = primary.id
    db.session.flush()

    # 4. Delete the loser Employee row.
    db.session.delete(loser)


def run(dry_run=True):
    groups = _find_duplicate_groups()
    if not groups:
        return {"duplicate_emails": 0, "merged_employees": 0, "plan": []}
    plan = []
    for g in groups:
        emps = sorted(g["employees"], key=lambda e: e.id)
        primary = emps[0]
        for loser in emps[1:]:
            plan.append({
                "company_id": g["company_id"],
                "email": g["email"],
                "primary_id": primary.id,
                "loser_id": loser.id,
                "primary_name": primary.name,
                "loser_name": loser.name,
            })
    if not dry_run:
        for step in plan:
            primary = db.session.get(Employee, step["primary_id"])
            loser = db.session.get(Employee, step["loser_id"])
            _merge_pair(primary, loser)
        db.session.commit()
    return {
        "duplicate_emails": len(groups),
        "merged_employees": len(plan),
        "plan": plan,
    }


@click.command("merge-duplicate-employees")
@click.option("--apply", is_flag=True,
              help="Actually merge (default: dry-run)")
@with_appcontext
def merge_cli(apply):
    """Find + merge Employee rows that share an email in the same company."""
    result = run(dry_run=not apply)
    tag = "APPLIED" if apply else "DRY-RUN"
    print(f"\n{tag}:")
    print(f"  (company, email) pairs with duplicates: {result['duplicate_emails']}")
    print(f"  loser employees to merge:               {result['merged_employees']}")
    if result.get("plan"):
        print("\n  Plan:")
        for step in result["plan"]:
            print(f"    company #{step['company_id']}  {step['email']}:")
            print(f"      keep     Employee #{step['primary_id']}  {step['primary_name']!r}")
            print(f"      merge in Employee #{step['loser_id']}  {step['loser_name']!r}")
    if not apply and result["merged_employees"] > 0:
        print("\nThis was a dry-run. Add --apply to write changes.")
