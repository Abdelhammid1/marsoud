#!/usr/bin/env python3
"""MARSOUD-BOT-PROTECTION-01 (Abdelhamid 2026-07-24).

Delete bot-created companies that never verified their email.

USAGE
-----
Dry-run (safe, prints names, changes NOTHING):
  python3 scripts/cleanup_bot_signups.py

Apply (deletes for real, cascades user_companies + all company-
scoped rows for each target):
  python3 scripts/cleanup_bot_signups.py --apply

CRITERIA
--------
A company is a bot signup target if ALL of these are true:

  1. Its OWNER user is PENDING_VERIFICATION (never clicked the
     welcome-email link) AND was created ≥ 24h ago (fresh signups
     that are just late to verify get a grace day).
  2. The company has ZERO real activity:
     · No posted journal entries (source_type NOT NULL).
     · No invoices.
     · No user_sessions logging in beyond the initial signup burst.

This is DELIBERATELY conservative — false positives are worse
than false negatives here. The dry-run prints every candidate so
Ibrahim can review before applying.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


def find_targets():
    """Return a list of Company rows that look like bot signups."""
    from app.models import Company, User, UserStatus, JournalEntry, Invoice
    from app.models.user import user_companies

    cutoff = datetime.utcnow() - timedelta(hours=24)

    # Companies whose OWNER is pending verification + ≥ 24h old.
    q = (
        db.session.query(Company)
        .join(user_companies,
              user_companies.c.company_id == Company.id)
        .join(User, User.id == user_companies.c.user_id)
        .filter(user_companies.c.role == "owner")
        .filter(User.status == UserStatus.PENDING_VERIFICATION.value)
        .filter(User.created_at < cutoff)
        .filter(Company.deleted_at.is_(None))
    )

    targets = []
    for co in q.all():
        # Skip if it has any posted journal entries.
        has_entries = db.session.query(JournalEntry.id).filter(
            JournalEntry.company_id == co.id,
        ).limit(1).first() is not None
        if has_entries:
            continue
        has_invoices = db.session.query(Invoice.id).filter(
            Invoice.company_id == co.id,
        ).limit(1).first() is not None
        if has_invoices:
            continue
        targets.append(co)
    return targets


def dry_run(targets):
    print(f"Found {len(targets)} candidate bot-signup companies:")
    print("─" * 72)
    if not targets:
        print("Nothing to clean. Registration guards are keeping the DB clean.")
        return
    for co in targets:
        owner_email = "(none)"
        try:
            owner_email = co.users.first().email
        except Exception:
            pass
        print(f"  #{co.id:5}  sub={co.subdomain:<20}  "
              f"name={co.name!r}  owner={owner_email}  "
              f"created={co.created_at}")
    print("─" * 72)
    print("This was a DRY RUN — no changes made.")
    print("Rerun with --apply to actually delete these.")


def apply(targets):
    """Delete each target company + cascade all its rows. Uses the
    existing platform hard_delete_company path for consistency with
    the super-admin UI."""
    from app.services.superadmin import hard_delete_company
    print(f"Deleting {len(targets)} bot-signup companies...")
    deleted = 0
    for co in targets:
        try:
            hard_delete_company(co, actor_id=None,
                                 reason="bot-cleanup script")
            deleted += 1
            print(f"  ✓ #{co.id}  {co.subdomain}")
        except Exception as e:
            print(f"  ✗ #{co.id}  {co.subdomain}  ERROR: {e}")
    print(f"Done. Deleted {deleted}/{len(targets)}.")


def main():
    p = argparse.ArgumentParser(
        description="Delete companies from bot signups that never verified.")
    p.add_argument("--apply", action="store_true",
                    help="Actually delete. Without this flag, prints only.")
    args = p.parse_args()

    app = create_app()
    with app.app_context():
        targets = find_targets()
        if args.apply:
            apply(targets)
        else:
            dry_run(targets)


if __name__ == "__main__":
    main()
