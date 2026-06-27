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
    """Super-admin permanent wipe. Logs FIRST (so the audit row survives),
    then drops the row. SQLAlchemy cascades to most related rows; any
    foreign-key blockage bubbles back to the caller for handling."""
    name = company.name
    cid = company.id
    db.session.add(PlatformAuditLog(
        actor_id=actor_id, action="company_hard_delete",
        target_company_id=cid,
        details=f"PERMANENT — {name} — reason: {(reason or '').strip() or '—'}",
    ))
    db.session.flush()
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
