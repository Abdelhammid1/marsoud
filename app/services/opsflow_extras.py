"""Helpers for the Cycle 7 gap-close work — notifications, audit, documents,
client feedback gating.
"""
import json
import os
import mimetypes
from pathlib import Path
from datetime import date, datetime, timedelta
from werkzeug.utils import secure_filename
from flask import current_app
from sqlalchemy import event

from app import db
from app.models import (
    Notification, NotificationKind,
    AuditEntry,
    Document, DocumentSourceType, DocumentVisibility,
    ClientFeedback,
    Lead, LeadStatus,
    Project, ProjectStatus, ProjectStatusEvent,
    Task, TaskStatus,
    User, Customer,
)
from app.models.user import user_companies


# ─── Notifications ──────────────────────────────────────────────────────
def notify(user_id, *, company_id, kind, title, body=None, link_url=None):
    """Insert a notification row. Commits the session (callers can rely on it
    being persisted independent of their own commit).

    `kind` should be a NotificationKind value (or a free-form string for
    forward-compat).
    """
    n = Notification(
        company_id=company_id, user_id=user_id,
        kind=kind.value if hasattr(kind, "value") else kind,
        title=title, body=body, link_url=link_url,
    )
    db.session.add(n)
    db.session.commit()
    return n


def notify_users(user_ids, **kwargs):
    out = []
    for uid in set(user_ids):
        if uid:
            out.append(notify(uid, **kwargs))
    return out


def mark_notification_read(notif):
    if notif.read_at is None:
        notif.read_at = datetime.utcnow()
        db.session.commit()
    return notif


def unread_count_for(user_id):
    return Notification.query.filter_by(user_id=user_id, read_at=None).count()


# ─── Audit Trail (generic) ──────────────────────────────────────────────
TRACKED_FIELDS = {
    "Lead": ["client_name", "email", "phone", "service_needed", "source",
             "assigned_to_id", "status", "next_meeting", "lost_reason"],
    "Project": ["name", "type", "customer_id", "manager_id", "start_date",
                "end_date", "status", "progress_pct"],
    "Task": ["title", "description", "project_id", "milestone_id",
             "assigned_to_id", "priority", "status", "deadline"],
}


def _record_audit(obj, action, changes=None):
    """Persist an AuditEntry row. Caller commits."""
    entity_type = type(obj).__name__
    company_id = getattr(obj, "company_id", None)
    if company_id is None:
        return
    # Prefer flask-login current_user; fallback to None for system-driven changes
    actor_id = None
    try:
        from flask_login import current_user
        if current_user and current_user.is_authenticated:
            actor_id = current_user.id
    except Exception:
        pass
    entry = AuditEntry(
        company_id=company_id,
        entity_type=entity_type,
        entity_id=getattr(obj, "id", 0) or 0,
        action=action,
        changed_by_id=actor_id,
        changes_json=json.dumps(changes, default=str, ensure_ascii=False) if changes else None,
    )
    db.session.add(entry)


_AUDIT_LISTENERS_REGISTERED = False


def init_audit_listeners(app=None):
    """Wire SQLAlchemy event listeners that emit AuditEntry rows on insert /
    update / delete of the tracked CRM models.

    Guarded so re-calling create_app() doesn't stack duplicate listeners
    (which would multiply the AuditEntry rows by N).
    """
    global _AUDIT_LISTENERS_REGISTERED
    if _AUDIT_LISTENERS_REGISTERED:
        return
    from sqlalchemy import inspect as sa_inspect

    def _diff(target):
        """Build a {field: [old, new]} dict for the tracked fields that changed."""
        entity = type(target).__name__
        fields = TRACKED_FIELDS.get(entity, [])
        if not fields:
            return None
        state = sa_inspect(target)
        out = {}
        for f in fields:
            attr = state.attrs.get(f)
            if not attr:
                continue
            hist = attr.history
            if hist.has_changes() and hist.added and hist.deleted:
                # Coerce Enum / Date / datetime to printable
                old = hist.deleted[0]
                new = hist.added[0]
                out[f] = [str(old) if old is not None else None,
                          str(new) if new is not None else None]
        return out or None

    def _after_insert(mapper, connection, target):
        if type(target).__name__ in TRACKED_FIELDS:
            _record_audit(target, "CREATE")

    def _after_update(mapper, connection, target):
        if type(target).__name__ in TRACKED_FIELDS:
            changes = _diff(target)
            if changes:
                _record_audit(target, "UPDATE", changes)

    def _after_delete(mapper, connection, target):
        if type(target).__name__ in TRACKED_FIELDS:
            _record_audit(target, "DELETE")

    for cls in (Lead, Project, Task):
        event.listen(cls, "after_insert", _after_insert, propagate=True)
        event.listen(cls, "after_update", _after_update, propagate=True)
        event.listen(cls, "after_delete", _after_delete, propagate=True)

    _AUDIT_LISTENERS_REGISTERED = True


# ─── Documents ──────────────────────────────────────────────────────────
# MARSOUD — supported attachment extensions. Includes iPhone-default
# HEIC/HEIF and modern image formats.
ALLOWED_EXTS = {"pdf", "png", "jpg", "jpeg", "gif", "webp", "heic", "heif",
                "doc", "docx", "xls", "xlsx", "zip"}
MAX_BYTES = 50 * 1024 * 1024   # 50 MB per spec NFR-11


class DocumentError(Exception):
    pass


def _extract_ext(original_filename: str) -> str:
    """MARSOUD bug-fix — extract the file extension from the *original*
    filename BEFORE running it through `secure_filename()`. Werkzeug's
    secure_filename strips non-ASCII characters (Arabic, emoji, etc.),
    which turns `"صورة.png"` into just `"png"` — the parser then thinks
    there's no extension and rejects the upload as "unsupported format".
    Splitting on the raw filename first dodges that completely."""
    if not original_filename or "." not in original_filename:
        return ""
    return original_filename.rsplit(".", 1)[-1].lower().strip()


def save_document(*, company_id, source_type, source_id, file_storage,
                  visibility="INTERNAL", uploaded_by_id=None, name=None):
    if not file_storage or not file_storage.filename:
        raise DocumentError("لم يُرفع أي ملف")
    # Read the extension from the ORIGINAL filename (before
    # secure_filename mangles non-ASCII characters away).
    original_filename = file_storage.filename
    ext = _extract_ext(original_filename)
    if ext not in ALLOWED_EXTS:
        raise DocumentError(
            "صيغة غير مدعومة. المسموح: "
            "PDF / صور (PNG, JPG, GIF, WEBP, HEIC) / Word / Excel / ZIP"
        )
    safe_filename = secure_filename(original_filename)
    # secure_filename may strip everything when the name was all-Arabic
    # (e.g. "صورة.png" → "png" or even ""). Re-synthesise so the on-disk
    # name is always identifiable.
    if not safe_filename or safe_filename == ext:
        safe_filename = f"file.{ext}"
    elif "." not in safe_filename:
        safe_filename = f"{safe_filename}.{ext}"

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_BYTES:
        raise DocumentError("الملف يتجاوز 50 ميجا — اضغطه أو قسّمه")

    src = source_type.value if hasattr(source_type, "value") else source_type
    if src not in ("LEAD", "PROJECT", "TASK"):
        raise DocumentError("نوع غير صالح")

    doc_dir = Path(current_app.root_path) / "static" / "docs" / str(company_id) / src.lower()
    doc_dir.mkdir(parents=True, exist_ok=True)
    # Unique filename — entity id + timestamp
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    safe_name = f"{source_id}_{ts}_{safe_filename}"
    disk = doc_dir / safe_name
    file_storage.save(str(disk))

    rel_url = f"/static/docs/{company_id}/{src.lower()}/{safe_name}"
    doc = Document(
        company_id=company_id,
        source_type=src,
        source_id=source_id,
        # Display name preserves the ORIGINAL (Arabic-friendly) filename
        # so the task detail page shows what the uploader actually saw.
        name=name or original_filename,
        file_path=rel_url,
        mimetype=(mimetypes.guess_type(safe_filename)[0]
                  or file_storage.mimetype),
        size_bytes=size,
        visibility=visibility.value if hasattr(visibility, "value") else visibility,
        uploaded_by_id=uploaded_by_id,
    )
    db.session.add(doc)
    db.session.commit()
    return doc


def documents_for(source_type, source_id, *, only_client_visible=False):
    src = source_type.value if hasattr(source_type, "value") else source_type
    q = Document.query.filter_by(source_type=src, source_id=source_id)
    if only_client_visible:
        q = q.filter_by(visibility="CLIENT")
    return q.order_by(Document.created_at.desc()).all()


def delete_document(doc):
    """Remove DB row + disk file."""
    p = Path(current_app.root_path) / doc.file_path.lstrip("/")
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
    db.session.delete(doc)
    db.session.commit()


# ─── ClientFeedback workflow ────────────────────────────────────────────
class FeedbackError(Exception):
    pass


def submit_client_feedback(project, *, customer_id, rating, comment=None,
                            submitted_by_user_id=None):
    if not (1 <= int(rating) <= 5):
        raise FeedbackError("التقييم يجب أن يكون من 1 إلى 5")
    fb = ClientFeedback(
        project_id=project.id, customer_id=customer_id,
        submitted_by_user_id=submitted_by_user_id,
        rating=int(rating),
        comment=(comment or "").strip() or None,
        approved=int(rating) >= 4,  # 4-5 = approved by default; admin can flip
    )
    db.session.add(fb)
    db.session.commit()

    # Notify the project manager
    if project.manager_id:
        notify(
            project.manager_id, company_id=project.company_id,
            kind=NotificationKind.PROJECT_FEEDBACK_SUBMITTED,
            title=f"ملاحظات جديدة على مشروع: {project.name}",
            body=f"التقييم: {rating}/5",
            link_url=f"/projects/{project.id}",
        )
    return fb


def project_has_approved_feedback(project):
    return any(fb.approved for fb in (project.feedback or []))


# ─── Project auto-delivery → Client Feedback request ────────────────────
def auto_request_feedback_on_delivery(project):
    """Called after project transitions to DELIVERED — auto-moves the project
    to CLIENT_FEEDBACK and notifies the client portal user(s)."""
    if project.status != ProjectStatus.DELIVERED:
        return
    # Move state forward + record event
    old = project.status
    project.status = ProjectStatus.CLIENT_FEEDBACK
    db.session.add(ProjectStatusEvent(
        project_id=project.id, from_status=old,
        to_status=ProjectStatus.CLIENT_FEEDBACK,
        changed_by_id=None, note="تلقائي: طلب ملاحظات العميل بعد التسليم",
    ))
    db.session.commit()
    # Notify any client user linked to this customer
    client_users = User.query.filter_by(
        linked_customer_id=project.customer_id, is_active=True,
    ).all()
    for u in client_users:
        notify(
            u.id, company_id=project.company_id,
            kind=NotificationKind.PROJECT_FEEDBACK_REQUESTED,
            title=f"مطلوب: ملاحظاتك على مشروع {project.name}",
            body="الرجاء فتح بوابة العميل لإرسال ملاحظاتك.",
            link_url=f"/portal/projects/{project.id}",
        )


# ─── Deadline reminders (called from /cron/tick) ────────────────────────
def remind_task_deadlines_24h():
    """Find tasks with deadline in (today, today+1] that aren't done/blocked
    and haven't been reminded already today. Notifies the assignee."""
    today = date.today()
    horizon = today + timedelta(days=1)
    candidates = Task.query.filter(
        Task.deadline.isnot(None),
        Task.deadline > today, Task.deadline <= horizon,
        Task.status.notin_([TaskStatus.DONE, TaskStatus.BLOCKED]),
    ).all()
    sent = 0
    for t in candidates:
        # Dedup: did we already send a 24h reminder for this task today?
        already = Notification.query.filter_by(
            user_id=t.assigned_to_id,
            kind=NotificationKind.TASK_DEADLINE_24H.value,
        ).filter(
            Notification.body.like(f"%task#{t.id}%")
            if hasattr(Notification.body, "like") else True
        ).count()
        if already:
            continue
        notify(
            t.assigned_to_id, company_id=t.company_id,
            kind=NotificationKind.TASK_DEADLINE_24H,
            title=f"⏰ Deadline قريب: {t.title}",
            body=f"المهمة task#{t.id} تنتهي يوم {t.deadline.isoformat()}.",
            link_url=f"/tasks/{t.id}",
        )
        sent += 1
    return {"checked": len(candidates), "reminders_sent": sent}
