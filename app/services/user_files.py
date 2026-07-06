"""MARSOUD-USER-FILES — save/stream/delete helpers for the per-user
folder feature.

Design notes:
  - Files live under app/private_uploads/user_files/<company>/<user>/
    so they are NOT publicly served by Flask's static handler. Every
    read passes through app/routes/user_files.py which enforces owner
    OR admin-with-users.view.
  - The on-disk name uses a uuid4 prefix so upload collisions are
    impossible even when two users upload the same original filename.
  - Uploads are size-capped at MAX_BYTES and extension-filtered.
    Rejected uploads never touch disk.
"""
import mimetypes
import os
import uuid
from pathlib import Path
from flask import current_app
from werkzeug.utils import secure_filename

from app import db
from app.models import UserFile


class UserFileError(Exception):
    """Raised for user-facing upload/delete failures."""


MAX_BYTES = 10 * 1024 * 1024   # 10 MB per the ticket scope answer
# Aggregate per-user cap: enough for a professional's document set
# without letting one employee fill the Railway volume. 500 MB
# equals ~50 max-sized uploads.
MAX_USER_QUOTA_BYTES = 500 * 1024 * 1024
# Anything the browser can render inline or reasonably wrap in a
# download link. Executables are refused so the folder can't be
# repurposed as a distribution channel for scripts/binaries.
ALLOWED_EXTS = {
    "pdf", "png", "jpg", "jpeg", "gif", "webp", "heic",
    "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "txt", "csv", "zip", "rar", "7z",
}


def _root() -> Path:
    """Base directory for private uploads. Created lazily so a fresh
    checkout doesn't need a manual mkdir step."""
    root = Path(current_app.root_path) / "private_uploads" / "user_files"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _extract_ext(original_filename: str) -> str:
    """Read the extension from the ORIGINAL filename before
    secure_filename mangles non-ASCII chars — same trick opsflow_extras
    uses for Arabic filenames like "صورة.png"."""
    if not original_filename or "." not in original_filename:
        return ""
    return original_filename.rsplit(".", 1)[-1].lower().strip()


def save_user_file(*, company_id: int, user_id: int, file_storage) -> UserFile:
    """Persist a Werkzeug FileStorage as a UserFile row. Rolls back the
    session on failure so the caller doesn't have to."""
    if not file_storage or not file_storage.filename:
        raise UserFileError("لم يُرفع أي ملف")

    original = file_storage.filename
    ext = _extract_ext(original)
    if ext not in ALLOWED_EXTS:
        raise UserFileError(
            "صيغة غير مدعومة. المسموح: PDF / صور / Word / Excel / "
            "PowerPoint / نص / ZIP"
        )

    # Size check — read into stream, tell(), rewind.
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_BYTES:
        raise UserFileError(
            f"الملف يتجاوز الحد الأقصى ({MAX_BYTES // (1024*1024)} ميجا)"
        )
    if size <= 0:
        raise UserFileError("الملف فارغ")

    # Aggregate-quota check — one employee shouldn't be able to fill
    # the volume by uploading many small files under the per-file cap.
    used = used_quota_bytes(company_id=company_id, user_id=user_id)
    if used + size > MAX_USER_QUOTA_BYTES:
        remaining_mb = max(0, MAX_USER_QUOTA_BYTES - used) // (1024*1024)
        raise UserFileError(
            f"تجاوزت حصة المجلد ({MAX_USER_QUOTA_BYTES // (1024*1024)} ميجا). "
            f"المتبقي {remaining_mb} ميجا — احذف ملفات قديمة قبل الرفع."
        )

    dest_dir = _root() / str(company_id) / str(user_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # uuid prefix guarantees uniqueness even for identical original names.
    key = f"{uuid.uuid4().hex}.{ext}"
    disk_path = dest_dir / key
    file_storage.save(str(disk_path))

    # Display name preserves the ORIGINAL filename (Arabic-friendly);
    # storage_key is the uuid-only opaque handle we resolve on read.
    row = UserFile(
        company_id=company_id, user_id=user_id,
        name=original,
        storage_key=f"{company_id}/{user_id}/{key}",
        mimetype=(mimetypes.guess_type(original)[0]
                   or file_storage.mimetype),
        size_bytes=size,
    )
    try:
        db.session.add(row)
        db.session.commit()
    except Exception:
        db.session.rollback()
        try:
            disk_path.unlink()
        except OSError:
            pass
        raise
    return row


def resolve_disk_path(user_file: UserFile) -> Path:
    """Absolute Path for a stored file. Route code uses this to hand
    the bytes to send_file — never expose the key to the client."""
    return _root() / user_file.storage_key


def delete_user_file(user_file: UserFile) -> None:
    """Remove DB row + disk file. Silent on missing disk entries so
    orphaned rows can be cleaned up without extra branching."""
    disk = resolve_disk_path(user_file)
    if disk.exists():
        try:
            disk.unlink()
        except OSError:
            pass
    db.session.delete(user_file)
    db.session.commit()


def files_for_user(*, company_id: int, user_id: int):
    """Owner's-eye list, newest first."""
    return (UserFile.query
             .filter_by(company_id=company_id, user_id=user_id)
             .order_by(UserFile.created_at.desc())
             .all())


def used_quota_bytes(*, company_id: int, user_id: int) -> int:
    """Sum of size_bytes for one user's files in one company. Used by
    the quota gate + surfaced in the UI so users see 'X of 500 MB'."""
    from sqlalchemy import func
    total = db.session.query(func.coalesce(func.sum(UserFile.size_bytes), 0)) \
        .filter(UserFile.company_id == company_id,
                UserFile.user_id == user_id) \
        .scalar()
    return int(total or 0)


def delete_all_for_user_in_company(*, company_id: int, user_id: int) -> int:
    """Cascade sweep called when a user is removed from a company —
    drop every DB row + disk file so we don't leave orphaned bytes
    under private_uploads/user_files/<company>/<user>/. Returns the
    number of rows deleted (for the caller's audit log)."""
    rows = UserFile.query.filter_by(
        company_id=company_id, user_id=user_id,
    ).all()
    deleted = 0
    for r in rows:
        disk = resolve_disk_path(r)
        if disk.exists():
            try:
                disk.unlink()
            except OSError:
                pass
        db.session.delete(r)
        deleted += 1
    # Also try to remove the now-empty user directory. Silent on
    # failure — the sweep is idempotent and the next run will pick
    # up anything we missed.
    user_dir = _root() / str(company_id) / str(user_id)
    if user_dir.exists():
        try:
            user_dir.rmdir()
        except OSError:
            pass
    db.session.commit()
    return deleted
