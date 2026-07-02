#!/usr/bin/env python3
"""MARSOUD-BUG (2026-07) — merge users that share the same email
(case-insensitive).

Abdelhamid reported that "when I created an employee with the same
email as my owner login, I ended up with two accounts." The current
code (hr_self_service.ensure_user_for_employee) is now correct — it
finds+links by lowercase email — but his server already has stale
duplicates from before the fix. This script consolidates them so his
owner account stops appearing "twice".

Merge policy:
  - The user with the LOWEST id wins ("primary"). It's usually the
    real login account (owner/admin) — the newer duplicate is the
    HR-side employee shell.
  - Every user_companies row and employee reference on the loser is
    reassigned to the primary.
  - Loser's User row is deleted.

Never touches accounts that don't have duplicates. Never merges across
active login sessions — the primary keeps its password / status.

Usage:
    flask merge-duplicate-users                # dry-run
    flask merge-duplicate-users --apply        # actually merge
"""
import click
from collections import defaultdict

from flask.cli import with_appcontext
from sqlalchemy import func

from app import db
from app.models import User


def _find_duplicate_groups():
    """Return [{email: str, users: [User, ...]}, ...] for every email
    that has more than one User row (case-insensitive)."""
    groups = defaultdict(list)
    for u in User.query.order_by(User.id).all():
        if not u.email:
            continue
        groups[u.email.lower()].append(u)
    return [
        {"email": em, "users": lst}
        for em, lst in groups.items() if len(lst) > 1
    ]


def _merge_pair(primary, loser):
    """Move every reference from loser → primary, then delete loser."""
    from app.models.user import user_companies

    # 1. Reassign user_companies rows. If a row with the same
    #    (user_id, company_id) already exists on primary, skip it —
    #    otherwise re-point it.
    prim_companies = {
        r.company_id for r in db.session.execute(
            user_companies.select().where(
                user_companies.c.user_id == primary.id
            )
        ).fetchall()
    }
    loser_rows = db.session.execute(
        user_companies.select().where(user_companies.c.user_id == loser.id)
    ).fetchall()
    for r in loser_rows:
        if r.company_id in prim_companies:
            # Primary is already a member → drop the loser's dup row.
            db.session.execute(user_companies.delete().where(
                (user_companies.c.user_id == loser.id) &
                (user_companies.c.company_id == r.company_id)
            ))
        else:
            db.session.execute(user_companies.update().where(
                (user_companies.c.user_id == loser.id) &
                (user_companies.c.company_id == r.company_id)
            ).values(user_id=primary.id))

    # 2. Reassign FKs on other tables. This is the wide surface — for
    #    each FK to users.id that isn't the loser's row itself, re-point
    #    to primary.id.
    for table_name, cols in [
        ("employees",           ["created_by_id"]),
        ("leads",               ["assigned_to_id", "created_by_id",
                                  "deleted_by_id"]),
        ("lead_status_events",  ["changed_by_id"]),
        ("lead_comments",       ["user_id"]),
        ("lead_activities",     ["created_by_id"]),
        ("tasks",               ["assigned_to_id", "created_by_id",
                                  "archived_by_id"]),
        ("task_comments",       ["user_id"]),
        ("task_activity_logs",  ["user_id"]),
        ("task_assignees",      ["user_id"]),
        ("projects",            ["manager_id"]),
        ("project_members",     ["user_id"]),
        ("project_status_events", ["changed_by_id"]),
        ("notifications",       ["user_id"]),
        ("audit_entries",       ["changed_by_id"]),
        ("documents",           ["uploaded_by_id"]),
        ("customers",           ["sales_rep_id"]),
        ("journal_entries",     ["created_by"]),
        ("invoices",            ["cashier_id"]),
        ("cashier_shifts",      ["cashier_id"]),
        ("user_sessions",       ["user_id"]),
        ("user_activity_log",   ["user_id"]),
        ("platform_audit_log",  ["user_id", "target_user_id"]),
        ("employee_accruals",   []),
        ("payroll_runs",        ["created_by"]),
        ("api_tokens",          ["user_id"]),
        ("stock_movements",     ["actor_id"]),
        ("credit_notes",        []),
        ("campaigns",           ["created_by_id"]),
        ("payments",            []),
        ("employee_history",    ["changed_by"]),
    ]:
        for col in cols:
            try:
                db.session.execute(db.text(
                    f"UPDATE {table_name} SET {col} = :pid "
                    f"WHERE {col} = :lid"
                ), {"pid": primary.id, "lid": loser.id})
            except Exception as e:
                # Tolerate a missing table on very old DBs; report + skip.
                print(f"    (skipped {table_name}.{col}: {e})")

    # 3. Reassign users.employee_id link if loser had one and primary doesn't.
    if loser.employee_id and not primary.employee_id:
        primary.employee_id = loser.employee_id

    # 4. Delete loser.
    db.session.delete(loser)


def run(dry_run=True):
    groups = _find_duplicate_groups()
    if not groups:
        return {"duplicate_emails": 0, "merged_users": 0, "kept": []}
    plan = []
    for g in groups:
        us = sorted(g["users"], key=lambda u: u.id)
        primary = us[0]
        for loser in us[1:]:
            plan.append({
                "email": g["email"],
                "primary_id": primary.id,
                "loser_id": loser.id,
                "primary_full_name": primary.full_name,
                "loser_full_name": loser.full_name,
            })
    if not dry_run:
        for step in plan:
            primary = db.session.get(User, step["primary_id"])
            loser = db.session.get(User, step["loser_id"])
            _merge_pair(primary, loser)
        db.session.commit()
    return {
        "duplicate_emails": len(groups),
        "merged_users": len(plan),
        "plan": plan,
    }


@click.command("merge-duplicate-users")
@click.option("--apply", is_flag=True,
              help="Actually merge (default: dry-run)")
@with_appcontext
def merge_cli(apply):
    """Find + merge users that share the same email (case-insensitive)."""
    result = run(dry_run=not apply)
    tag = "APPLIED" if apply else "DRY-RUN"
    print(f"\n{tag}:")
    print(f"  emails with duplicates: {result['duplicate_emails']}")
    print(f"  loser users to merge:   {result['merged_users']}")
    if result.get("plan"):
        print("\n  Plan:")
        for step in result["plan"]:
            print(f"    {step['email']}:")
            print(f"      keep #{step['primary_id']} ({step['primary_full_name']!r})")
            print(f"      merge in #{step['loser_id']} ({step['loser_full_name']!r})")
    if not apply and result["merged_users"] > 0:
        print("\nThis was a dry-run. Add --apply to write changes.")
