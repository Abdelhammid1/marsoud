#!/usr/bin/env python3
"""MARSOUD-OPS-FOUNDATION (2026-08-05) — give EXISTING companies the two
accounts the next wave of accounting operations needs.

    1170  إيرادات مستحقة          ASSET    under 1100
    5940  فوائد وأعباء تمويلية    EXPENSE  under 5900

Adding them to DEFAULT_COA only serves companies created from now on:
`seed_default_coa` runs once, at company creation (routes/auth.py,
routes/companies.py, seed.py) and there is no re-seed path. Without this
script the new operations would work for new tenants and fail for every
existing one — a fault that surfaces late and looks random.

WHY THIS SCRIPT REFUSES TO BE CLEVER
====================================
It adds THESE TWO CODES AND NOTHING ELSE. There is deliberately no "diff
the company against DEFAULT_COA and add what's missing" logic here, and
none should be added later.

Company 8 has a chart of accounts built from scratch. It differs from the
default tree on purpose: accounts the owner deleted are meant to stay
deleted, and some custom accounts sit on codes the default tree uses for
something else entirely. A full sync would resurrect dozens of removed
accounts and could collide with those custom ones. Any company that has
edited its tree has the same exposure.

For the same reason, a company whose 1170 or 5940 is already taken by a
DIFFERENT account is skipped and reported, never overwritten.

Usage:
    flask backfill-ops-accounts                    # dry-run, all companies
    flask backfill-ops-accounts --company-id 8     # dry-run, one company
    flask backfill-ops-accounts --apply            # write
"""
import click
from flask.cli import with_appcontext

from app import db
from app.models import Account, AccountType, Company
from app.models.account import NORMAL_SIDE_FOR_TYPE


# (code, name_en, name_ar, type, parent_code). Exactly the two rows added
# to DEFAULT_COA in app/services/seed_coa.py — keep them in step.
NEW_ACCOUNTS = [
    ("1170", "Accrued Revenue", "إيرادات مستحقة",
     AccountType.ASSET, "1100"),
    ("5940", "Interest & Financing Charges", "فوائد وأعباء تمويلية",
     AccountType.EXPENSE, "5900"),
]


def run(dry_run=True, company_id=None):
    """Add the two accounts to companies that lack them.

    Returns a summary dict:
        companies       companies examined
        added           [(company_id, code)] actually inserted (0 on dry-run)
        would_add       [(company_id, code)] the plan
        already_there   [(company_id, code)] nothing to do
        code_taken      [(company_id, code, existing_name)] SKIPPED — the
                        code belongs to a different account in that company
        no_parent       [(company_id, code, parent_code)] SKIPPED — the
                        parent header is missing from that company's tree
    """
    q = Company.query
    if company_id is not None:
        q = q.filter(Company.id == company_id)
    companies = q.order_by(Company.id).all()

    added, would_add, already_there, code_taken, no_parent = [], [], [], [], []

    for co in companies:
        # One query per company; these trees are ~100 rows.
        by_code = {a.code: a for a in Account.query.filter_by(
            company_id=co.id).all()}

        for code, name_en, name_ar, atype, parent_code in NEW_ACCOUNTS:
            existing = by_code.get(code)
            if existing is not None:
                # Is it OURS, or has this company put something else here?
                if (existing.name_ar or "").strip() == name_ar or \
                        (existing.name or "").strip() == name_en:
                    already_there.append((co.id, code))
                else:
                    code_taken.append(
                        (co.id, code, existing.name_ar or existing.name))
                continue

            parent = by_code.get(parent_code)
            if parent is None:
                no_parent.append((co.id, code, parent_code))
                continue

            would_add.append((co.id, code))
            if dry_run:
                continue

            db.session.add(Account(
                company_id=co.id, code=code, name=name_en, name_ar=name_ar,
                type=atype, normal_side=NORMAL_SIDE_FOR_TYPE[atype],
                parent_id=parent.id, is_active=True,
                # Leaves take journal lines; a header would refuse them at
                # post time, which is the failure this script exists to
                # prevent.
                is_postable=True,
            ))
            added.append((co.id, code))

    if not dry_run and added:
        db.session.commit()

    return {
        "companies": len(companies),
        "added": added,
        "would_add": would_add,
        "already_there": already_there,
        "code_taken": code_taken,
        "no_parent": no_parent,
    }


@click.command("backfill-ops-accounts")
@click.option("--company-id", type=int, default=None,
              help="Limit to one company (default: all).")
@click.option("--apply", is_flag=True,
              help="Actually insert the accounts (default: dry-run).")
@with_appcontext
def backfill_cli(company_id, apply):
    """Add 1170 + 5940 to existing companies that lack them."""
    result = run(dry_run=not apply, company_id=company_id)
    tag = "APPLIED" if apply else "DRY-RUN"
    scope = f"company {company_id}" if company_id else "all companies"

    # ASCII only - this runs on Windows consoles (cp1252) too.
    print("\n" + "-" * 60)
    print(f"{tag} - ops foundation accounts, {scope}")
    print("-" * 60)
    print(f"companies examined : {result['companies']}")
    print(f"already present    : {len(result['already_there'])}")
    print(f"to add             : {len(result['would_add'])}")
    print(f"inserted           : {len(result['added'])}")

    if result["would_add"]:
        print("\nplan:")
        for cid, code in result["would_add"]:
            print(f"  company {cid:>4}  + {code}")

    if result["code_taken"]:
        print("\n!! code already used by a DIFFERENT account - SKIPPED, "
              "nothing was touched:")
        for cid, code, name in result["code_taken"]:
            print(f"  company {cid:>4}  {code} is '{name}'")

    if result["no_parent"]:
        print("\n!! parent header missing - SKIPPED, nothing was touched:")
        for cid, code, parent in result["no_parent"]:
            print(f"  company {cid:>4}  {code} needs parent {parent}")

    if not apply and result["would_add"]:
        print("\nThis was a dry-run. Add --apply to write changes.")
    print()
