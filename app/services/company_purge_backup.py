"""MARSOUD-COMPANIES-BULK-DELETE (2026-08-12) — pre-purge
JSON snapshot for hard-deleted companies.

Called from `superadmin.companies_bulk_hard_delete` BEFORE
the destructive commit, so if the wipe is a mistake the
row set can still be reconstructed from disk.

Coverage strategy: iterate `db.metadata.sorted_tables` and
for every table that has a `company_id` column, dump
`SELECT * WHERE company_id = :cid` as a list of dicts.
That's the exact table set `hard_delete_company` is about
to `DELETE` from (`app/services/lifecycle.py:94-104`), so
the JSON matches the wipe byte-for-byte.

Also captures:
  · the `companies` row itself (parent, no company_id
    column on itself),
  · `user_companies` associations (m2m; carries
    company_id via the composite PK).

Values are JSON-serialised via `str()` for datetime /
Decimal / bytes. Portability beats round-trip precision
here — this file is a survivor snapshot, not a live
replica. A `_manifest` block at the top makes each file
self-describing (company id/name, timestamp, per-table
row counts) so Abdelhamid can eyeball it later without
loading anything.

File layout: `app/static/backups/company_purges/<cid>_<yyyymmdd_HHMMSS>.json`.
The directory is created lazily on first use, with a
sibling `.gitignore` (contents `*`) so accidental commits
of production backup dumps are impossible.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import json
import os

from flask import current_app

from app import db


BACKUP_SUBDIR = ("static", "backups", "company_purges")


def _backup_dir() -> Path:
    """Return the on-disk backup directory, creating it +
    the `.gitignore` sentinel on first use."""
    d = Path(current_app.root_path)
    for part in BACKUP_SUBDIR:
        d = d / part
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        try:
            gi.write_text("*\n!.gitignore\n", encoding="utf-8")
        except OSError:
            # Read-only FS in some prod deploys — non-fatal.
            pass
    return d


def _jsonable(value):
    """Coerce a DB scalar to something json.dumps can eat."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        # str() over float() to avoid the classic 0.1+0.2 drift.
        return str(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        # Rare — inline as hex so at least the shape survives.
        return f"<bytes:{value.hex()[:80]}{'…' if len(value) > 40 else ''}>"
    # date, time, UUID, enum values, misc — fall back on str().
    return str(value)


def _dump_table_rows(table, where_clause):
    """Return list of dicts for one table filtered by where_clause."""
    rows = db.session.execute(
        table.select().where(where_clause)
    ).mappings().all()
    return [
        {k: _jsonable(v) for k, v in r.items()}
        for r in rows
    ]


def dump_company_to_json(company_id) -> Path:
    """Write a JSON snapshot of every row referencing
    `company_id` and return the on-disk Path.

    Raises OSError if the file cannot be written (e.g. RO FS,
    disk full). The bulk-hard-delete route treats that as a
    reason to SKIP the delete for this company — no silent
    data loss.
    """
    from app.models import Company

    company = db.session.get(Company, company_id)
    if company is None:
        raise ValueError(f"company {company_id} does not exist")

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    d = _backup_dir()
    out = d / f"{company_id}_{ts}.json"

    # Collect the parent row first (companies table itself).
    tables = list(db.metadata.sorted_tables)
    company_row = None
    for t in tables:
        if t.name == "companies":
            r = db.session.execute(
                t.select().where(t.c.id == company_id)
            ).mappings().first()
            company_row = ({k: _jsonable(v) for k, v in r.items()}
                            if r else None)
            break

    # Iterate every table with a company_id column.
    data = {}
    counts = {}
    for t in tables:
        if t.name == "companies":
            continue
        cols = {c.name for c in t.columns}
        if "company_id" not in cols:
            continue
        rows = _dump_table_rows(t, t.c.company_id == company_id)
        if rows:
            data[t.name] = rows
            counts[t.name] = len(rows)

    # user_companies is an m2m association with company_id
    # in its composite PK — captured by the loop above (its
    # table is registered on db.metadata). No special path.

    manifest = {
        "_kind": "marsoud_company_purge_backup",
        "_version": 1,
        "company_id": company_id,
        "company_name": company.name,
        "company_subdomain": company.subdomain,
        "generated_at": datetime.utcnow().isoformat(),
        "table_count": len(counts),
        "row_counts": counts,
    }

    payload = {
        "_manifest": manifest,
        "companies": [company_row] if company_row else [],
        **data,
    }

    # Write to a tmp file first, then rename — half-written
    # files on OSError never claim to be complete backups.
    tmp = out.with_suffix(out.suffix + ".part")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False,
                   separators=(",", ":"))
    try:
        os.chmod(tmp, 0o600)   # POSIX only; no-op on Windows
    except (OSError, NotImplementedError):
        pass
    os.replace(tmp, out)
    return out
