"""Soft-delete + restore + permanent-delete helpers for Company + Project.

Owner-driven actions go through soft_delete_*. Super-admin can restore or
hard-purge. Every transition writes a PlatformAuditLog row so the audit
trail captures who killed (or revived) what, and why.
"""
from datetime import datetime
from app import db
from app.models import Company, Project, PlatformAuditLog


# ─── Company lifecycle ────────────────────────────────────────────────────
def soft_delete_company(company, *, actor_id, reason):
    """Owner-triggered soft delete. Hides the company from the active
    switcher + dashboards. Restorable by super-admin."""
    if company.deleted_at is not None:
        return False
    company.deleted_at = datetime.utcnow()
    company.deleted_by_id = actor_id
    company.deletion_reason = (reason or "").strip() or None
    # is_active is the legacy "soft suspend" flag — flip it too so any
    # existing query that only checks is_active still skips the row.
    company.is_active = False
    db.session.add(PlatformAuditLog(
        actor_id=actor_id, action="company_soft_delete",
        target_company_id=company.id,
        details=f"reason: {company.deletion_reason or '—'}",
    ))
    db.session.commit()
    return True


def restore_company(company, *, actor_id):
    """Super-admin reversal of a soft delete."""
    if company.deleted_at is None:
        return False
    company.deleted_at = None
    company.deleted_by_id = None
    company.deletion_reason = None
    company.is_active = True
    db.session.add(PlatformAuditLog(
        actor_id=actor_id, action="company_restore",
        target_company_id=company.id,
        details=f"restored: {company.name}",
    ))
    db.session.commit()
    return True


def hard_delete_company(company, *, actor_id, reason):
    """Super-admin permanent wipe.

    Naïve `db.session.delete(company)` fails on every NOT-NULL
    `company_id` FK that lacks `ondelete=CASCADE` — Abdelhamid hit
    `customers.company_id` first, but the same trap exists across
    ~45 tables. We walk db.metadata.sorted_tables in REVERSE (children
    before parents) and run a bulk `DELETE WHERE company_id = ?` on
    every table that carries the column.

    MARSOUD-POS-ORPHAN-CASCADE (Abdelhamid 2026-07-22) — the older
    version of this docstring claimed "invoice_items → invoices
    cascade-delete via their own FKs." That was wrong: those FKs
    had no ON DELETE CASCADE at the DB level, so the bulk delete
    below LEFT the child rows orphaned. When a new invoice later
    got the same primary key (SQLite always, Postgres after a
    sequence reset / backup restore), SQLAlchemy's `.items`
    relationship auto-adopted the orphans, which is what surfaced
    on Abdelhamid's invoice 82 (a one-variant `create_pos_order`
    returned an invoice with two lines). The migration
    a6c9f2e5b8d1 added `company_id` to the three previously-blind
    child tables (invoice_items, payments, invoice_reminders_sent)
    so the loop below now catches them directly, and added
    ON DELETE CASCADE on every invoices-child FK as a second net.

    Logs the PlatformAuditLog row FIRST so the audit trace survives
    even if a downstream cascade fails. Per-table failures are
    collected into the PAL details for forensic value.
    """
    from sqlalchemy.exc import IntegrityError
    name = company.name
    cid = company.id

    db.session.add(PlatformAuditLog(
        actor_id=actor_id, action="company_hard_delete",
        target_company_id=cid,
        details=f"PERMANENT — {name} — reason: {(reason or '').strip() or '—'}",
    ))
    db.session.flush()

    failures = []
    rows_deleted = {}
    # children-first: iterate sorted_tables in reverse so the FK
    # parents (with company_id) come AFTER their dependent rows.
    for table in reversed(list(db.metadata.sorted_tables)):
        if table.name == "companies":
            continue
        if "company_id" not in {c.name for c in table.columns}:
            continue
        try:
            r = db.session.execute(
                table.delete().where(table.c.company_id == cid)
            )
            if r.rowcount:
                rows_deleted[table.name] = r.rowcount
        except IntegrityError as e:
            failures.append(f"{table.name}: {str(e)[:120]}")
            db.session.rollback()
            # Re-emit the PAL row (rollback dropped it) so the trace
            # of the attempted wipe + the table that blocked is kept.
            db.session.add(PlatformAuditLog(
                actor_id=actor_id, action="company_hard_delete_failed",
                target_company_id=cid,
                details=(f"PERMANENT — {name} — reason: "
                          f"{(reason or '').strip() or '—'} — "
                          f"blocked by {table.name}: {str(e)[:120]}"),
            ))
            db.session.commit()
            raise
    # All direct children purged; now drop the company row itself.
    db.session.delete(company)
    db.session.commit()
    return name


# ─── Project lifecycle ────────────────────────────────────────────────────
def soft_delete_project(project, *, actor_id, reason):
    """Owner-triggered project soft delete. Tasks + comments + activity
    are preserved. Project disappears from lists; detail page returns
    404 to non-superadmin readers."""
    if project.deleted_at is not None:
        return False
    project.deleted_at = datetime.utcnow()
    project.deleted_by_id = actor_id
    project.deletion_reason = (reason or "").strip() or None
    db.session.add(PlatformAuditLog(
        actor_id=actor_id, action="project_soft_delete",
        target_company_id=project.company_id,
        details=f"project_id={project.id} name={project.name!r} "
                f"reason: {project.deletion_reason or '—'}",
    ))
    db.session.commit()
    return True


def restore_project(project, *, actor_id):
    """Super-admin reversal of a project soft delete."""
    if project.deleted_at is None:
        return False
    project.deleted_at = None
    project.deleted_by_id = None
    project.deletion_reason = None
    db.session.add(PlatformAuditLog(
        actor_id=actor_id, action="project_restore",
        target_company_id=project.company_id,
        details=f"project_id={project.id} restored",
    ))
    db.session.commit()
    return True
