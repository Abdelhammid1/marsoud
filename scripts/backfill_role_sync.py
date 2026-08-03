#!/usr/bin/env python3
"""MARSOUD-ROLE-SYNC (2026-08-03) — repair user_companies rows where the
legacy `role` string and the `role_id` FK disagree.

`invitations.accept()` used to write only the string column. A member who
had since been promoted through the roles page and then re-opened any
invitation link for the same company got their string stomped back to the
invite's role while role_id kept the new one. Permission checks read both
(_db_has_permission wins when role_id is set; a long tail of pages still
branch on the string via get_user_role), so those users saw half of their
role — reported for زياد وائل and بسيم فكري on company 8.

The write path is fixed in app/services/roles.set_membership_role. This
script heals the rows that are already broken. `role_id` is the source of
truth: we rewrite `role` to the code of the linked Role.

The existing boot-time backfill (roles_seed.backfill_user_companies_role_ids)
only fills rows where role_id IS NULL, so it can never repair these.

Usage:
    flask backfill-role-sync                    # dry-run, all companies
    flask backfill-role-sync --company-id 8     # dry-run, one company
    flask backfill-role-sync --apply            # write
"""
import click
from flask.cli import with_appcontext

from app import db
from app.models import Role
from app.models.user import user_companies


def run(dry_run=True, company_id=None):
    """Find (and optionally fix) rows where role != roles.code.

    Returns a summary dict:
        scanned          rows with role_id NOT NULL that were examined
        mismatched       rows whose string disagreed with the FK
        fixed            rows actually rewritten (0 on a dry-run)
        orphan_role_id   role_id pointing at a missing Role row
        cross_company    role_id pointing at a Role in another company
        null_role_id     rows still on the legacy string only (informational)
        plan             [(user_id, company_id, old, new), ...]
    """
    sel = user_companies.select()
    if company_id is not None:
        sel = sel.where(user_companies.c.company_id == company_id)
    rows = db.session.execute(sel).fetchall()

    roles_by_id = {r.id: r for r in Role.query.all()}

    scanned = 0
    null_role_id = 0
    orphan = []
    cross_company = []
    plan = []

    for row in rows:
        if row.role_id is None:
            null_role_id += 1
            continue
        scanned += 1
        role = roles_by_id.get(row.role_id)
        if role is None:
            orphan.append((row.user_id, row.company_id, row.role_id))
            continue
        if role.company_id != row.company_id:
            # Never auto-fix this — it means the membership points at
            # another tenant's role and needs a human to look at it.
            cross_company.append((row.user_id, row.company_id, row.role_id))
            continue
        if (row.role or "") != role.code:
            plan.append((row.user_id, row.company_id, row.role or "", role.code))

    fixed = 0
    if not dry_run and plan:
        for user_id, cid, _old, new_code in plan:
            db.session.execute(
                user_companies.update().where(
                    (user_companies.c.user_id == user_id) &
                    (user_companies.c.company_id == cid)
                ).values(role=new_code)
            )
            fixed += 1
        db.session.commit()

    return {
        "scanned": scanned,
        "mismatched": len(plan),
        "fixed": fixed,
        "orphan_role_id": orphan,
        "cross_company": cross_company,
        "null_role_id": null_role_id,
        "plan": plan,
    }


@click.command("backfill-role-sync")
@click.option("--company-id", type=int, default=None,
              help="Limit to one company (default: all).")
@click.option("--apply", is_flag=True,
              help="Actually rewrite the role strings (default: dry-run).")
@with_appcontext
def backfill_cli(company_id, apply):
    """Re-sync user_companies.role with the role_id FK."""
    result = run(dry_run=not apply, company_id=company_id)
    tag = "APPLIED" if apply else "DRY-RUN"
    scope = f"company {company_id}" if company_id else "all companies"

    # ASCII only — this runs on Windows consoles (cp1252) too.
    print("\n" + "-" * 60)
    print(f"{tag} - role/role_id sync, {scope}")
    print("-" * 60)
    print(f"rows with role_id set : {result['scanned']}")
    print(f"rows still string-only: {result['null_role_id']}  "
          f"(handled at boot by ensure_roles_ready_for_company)")
    print(f"mismatched            : {result['mismatched']}")
    print(f"corrected             : {result['fixed']}")

    if result["plan"]:
        print("\nmismatched rows:")
        for user_id, cid, old, new in result["plan"]:
            print(f"  user {user_id:>5}  company {cid:>4}  "
                  f"'{old}' -> '{new}'")

    for label, key in (("orphan role_id (Role row missing)", "orphan_role_id"),
                       ("role_id from another company", "cross_company")):
        if result[key]:
            print(f"\n!! {label} - NOT touched, needs a human:")
            for user_id, cid, rid in result[key]:
                print(f"  user {user_id:>5}  company {cid:>4}  role_id {rid}")

    if not apply and result["mismatched"] > 0:
        print("\nThis was a dry-run. Add --apply to write changes.")
    print()
