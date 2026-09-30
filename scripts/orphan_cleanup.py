#!/usr/bin/env python3
"""MARSOUD-DB-ORPHAN-CLEANUP-01 (2026-09-30) — operator utility that
scans every declared foreign key in the ORM metadata and reports (or
deletes) child rows whose parent no longer exists.

Motivation
----------
Marsoud ran on SQLite for years with the default `PRAGMA
foreign_keys=OFF`, so `ondelete` clauses on the model layer were
silently no-op.  Now that `4c0ca97` turned the PRAGMA on globally, a
data review counted ~90k live orphans across 40+ tables — mostly
`role_permissions` rows pointing at deleted roles + `platform_audit_logs`
rows pointing at deleted users + some real `journal_lines` pointing at
deleted `accounts`.  This script surfaces + cleans them.

Marsoud is on SQLite (see `config.py:26`); PostgreSQL / MySQL don't
have this class of debt because they enforce FKs natively.  The script
queries via SQLAlchemy so it also runs on PG for completeness — the
SQL is standard.

Usage
-----
    # Report only, prints per-table counts + sample IDs.
    python scripts/orphan_cleanup.py

    # Same as above (--dry-run is the default).
    python scripts/orphan_cleanup.py --dry-run

    # Actually delete every orphan group found.
    python scripts/orphan_cleanup.py --apply

    # Limit to one child table (surgical).  Repeatable.
    python scripts/orphan_cleanup.py --apply --table journal_lines

    # Show noisy tables the operator already trusts and can skip.
    python scripts/orphan_cleanup.py --skip role_permissions --skip platform_audit_logs

Each `--apply` deletion runs in its OWN transaction so a failure on
one table (e.g. a FK RESTRICT from a grandchild that also needs
cleaning first) doesn't roll back the tables that already succeeded.
Failures are logged and reported at the bottom.

The three high-volume tables the ticket flagged
(`role_permissions`, `platform_audit_logs`, `journal_lines`) get a
spot-check at the top so an operator can eyeball them without
scrolling through the full 40-table report.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app import create_app, db


# Ticket-flagged tables the operator wants surfaced first.
SPOTLIGHT_TABLES = ("role_permissions", "platform_audit_logs", "journal_lines")


def enumerate_fks() -> list[tuple[str, str, str, str]]:
    """Walk `db.metadata` and return a list of
    `(child_table, child_col, parent_table, parent_col)` tuples for
    every declared FK.  Composite FKs are split into their columns
    (SQLite doesn't support composite FKs anyway)."""
    rows: list[tuple[str, str, str, str]] = []
    for table in db.metadata.sorted_tables:
        for fk in table.foreign_keys:
            child_col = fk.parent.name
            parent_col = fk.column.name
            parent_table = fk.column.table.name
            rows.append((table.name, child_col, parent_table, parent_col))
    return rows


def orphan_ids(child_table: str, child_col: str,
               parent_table: str, parent_col: str,
               limit: int | None = None) -> list[int]:
    """SELECT child.id FROM child LEFT JOIN parent ON parent.pk=child.fk
    WHERE parent.pk IS NULL AND child.fk IS NOT NULL."""
    q = (
        f"SELECT c.rowid FROM {child_table} c "
        f"LEFT JOIN {parent_table} p ON p.{parent_col} = c.{child_col} "
        f"WHERE c.{child_col} IS NOT NULL AND p.{parent_col} IS NULL"
    )
    if limit:
        q += f" LIMIT {int(limit)}"
    try:
        rows = db.session.execute(text(q)).fetchall()
    except SQLAlchemyError as e:
        print(f"    !! query failed for {child_table}.{child_col}: "
              f"{type(e).__name__}: {str(e.orig)[:120] if hasattr(e, 'orig') else e}")
        return []
    return [r[0] for r in rows]


def delete_orphans(child_table: str, orphan_rowids: list[int]) -> int:
    """DELETE FROM child_table WHERE rowid IN (?, ?, …).  Runs in its
    OWN nested transaction; on error, rolls back only that table.

    Returns the number of rows actually deleted, or -1 on failure."""
    if not orphan_rowids:
        return 0
    # Chunk into batches of 500 so a huge orphan set doesn't blow the
    # SQLite parameter limit.
    total_deleted = 0
    try:
        for chunk_start in range(0, len(orphan_rowids), 500):
            chunk = orphan_rowids[chunk_start:chunk_start + 500]
            placeholders = ",".join(f":r{i}" for i in range(len(chunk)))
            params = {f"r{i}": rid for i, rid in enumerate(chunk)}
            r = db.session.execute(
                text(f"DELETE FROM {child_table} WHERE rowid IN ({placeholders})"),
                params,
            )
            total_deleted += r.rowcount or 0
        db.session.commit()
        return total_deleted
    except SQLAlchemyError as e:
        db.session.rollback()
        print(f"    !! delete failed for {child_table}: "
              f"{type(e).__name__}: {str(e.orig)[:120] if hasattr(e, 'orig') else e}")
        return -1


def scan(fk_rows: Iterable[tuple[str, str, str, str]],
         only_tables: set[str] | None,
         skip_tables: set[str],
         sample: int = 5) -> dict:
    """Return `{(child_table, child_col): {"parent": ..., "count": N, "sample": [ids...]}}`."""
    report: dict = {}
    for child_table, child_col, parent_table, parent_col in fk_rows:
        if only_tables and child_table not in only_tables:
            continue
        if child_table in skip_tables:
            continue
        ids = orphan_ids(child_table, child_col, parent_table, parent_col)
        if not ids:
            continue
        key = (child_table, child_col)
        # Merge if the same child_col has multiple FKs (rare) — union
        # the ids and remember which parents were checked.
        entry = report.setdefault(key, {
            "parents": [], "ids": []})
        entry["parents"].append(f"{parent_table}.{parent_col}")
        entry["ids"].extend(ids)
    for key, entry in report.items():
        entry["ids"] = sorted(set(entry["ids"]))
        entry["count"] = len(entry["ids"])
        entry["sample"] = entry["ids"][:sample]
    return report


def print_report(report: dict, title: str) -> None:
    if not report:
        print(f"  ({title}: nothing found)")
        return
    print(f"  {title}:")
    for (child_table, child_col), entry in sorted(
        report.items(), key=lambda x: -x[1]["count"]
    ):
        parents = ", ".join(entry["parents"])
        print(f"    {child_table}.{child_col} -> {parents}"
              f"  |  {entry['count']:>6} orphans"
              f"  |  sample={entry['sample']}")


def main():
    ap = argparse.ArgumentParser(
        description="Scan (and optionally delete) orphan FK rows.")
    ap.add_argument("--apply", action="store_true",
                     help="Actually delete the orphan rows.  Without this "
                          "only reports.")
    ap.add_argument("--dry-run", action="store_true",
                     help="Explicit report-only mode.  Same as omitting "
                          "--apply.")
    ap.add_argument("--table", action="append", default=[],
                     help="Limit to one child table.  Repeatable.")
    ap.add_argument("--skip", action="append", default=[],
                     help="Skip a child table entirely.  Repeatable.")
    ap.add_argument("--sample", type=int, default=5,
                     help="How many sample IDs to show per orphan group.")
    args = ap.parse_args()

    if args.apply and args.dry_run:
        ap.error("--apply and --dry-run are mutually exclusive.")

    only_tables = set(args.table) if args.table else None
    skip_tables = set(args.skip)

    app = create_app()
    with app.app_context():
        dialect = db.engine.dialect.name
        db_url = str(db.engine.url).split("?")[0]
        print(f"database: {db_url} (dialect={dialect})")
        print(f"mode: {'APPLY (destructive)' if args.apply else 'dry-run'}")
        if only_tables:
            print(f"tables: only {sorted(only_tables)}")
        if skip_tables:
            print(f"skipping: {sorted(skip_tables)}")
        print()

        fk_rows = enumerate_fks()
        print(f"walked {len(fk_rows)} foreign-key declarations across "
              f"{len({r[0] for r in fk_rows})} tables")
        print()

        # Spotlight the three tables the ticket named.
        spotlight = [r for r in fk_rows if r[0] in SPOTLIGHT_TABLES]
        if spotlight and not only_tables:
            print("── spotlight (ticket-flagged tables) ──")
            spot_report = scan(spotlight, None, skip_tables, args.sample)
            print_report(spot_report, "orphan groups")
            print()

        # Full sweep — skip the spotlight tables in the full report so
        # they're not double-printed.
        full_report = scan(
            fk_rows, only_tables,
            skip_tables | set(SPOTLIGHT_TABLES if not only_tables else []),
            args.sample,
        )
        print("── full sweep ──")
        print_report(full_report, "orphan groups")
        print()

        # Merge both reports for the actual delete step (apply mode).
        merged: dict = {}
        for src in (spot_report if spotlight and not only_tables else {},
                    full_report):
            for key, entry in src.items():
                if key in merged:
                    merged[key]["ids"] = sorted(set(
                        merged[key]["ids"] + entry["ids"]))
                    merged[key]["count"] = len(merged[key]["ids"])
                else:
                    merged[key] = dict(entry)

        total = sum(e["count"] for e in merged.values())
        print(f"── total: {total} orphan rows across "
              f"{len(merged)} groups ──")

        if not args.apply:
            print("\nRe-run with --apply to delete.")
            return 0

        print("\nApplying deletes (per-table transactions)…")
        deleted_total = 0
        failures = 0
        for (child_table, child_col), entry in sorted(
            merged.items(), key=lambda x: -x[1]["count"]
        ):
            n = delete_orphans(child_table, entry["ids"])
            if n < 0:
                failures += 1
                print(f"  FAIL {child_table} ({entry['count']} planned)")
            else:
                deleted_total += n
                print(f"  ok   {child_table}: {n} rows deleted")
        print()
        print(f"done. {deleted_total} rows deleted; {failures} tables failed.")
        return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
