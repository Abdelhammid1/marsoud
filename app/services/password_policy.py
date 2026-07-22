"""MARSOUD-PASSWORD-POLICY (Abdelhamid 2026-07-22).

Central validation for every password entry point in the app:
signup, super-admin password reset, HR employee password set,
self-service password change, invitation acceptance, and (once
Ticket F ships) the /auth/forgot-password → reset-password flow.

Rules (deliberately conservative — we can tighten later):
  · Minimum 8 characters.
  · At least one letter (any alphabet, so Arabic + Latin both count).
  · At least one digit.

Returns (ok: bool, reason_ar: str). The Arabic reason is user-safe
copy — routes just flash it directly.
"""
import re

MIN_LENGTH = 8

# `\d` matches any Unicode digit; `[^\W\d_]` matches any letter in
# any script (Arabic, Latin, etc.) but excludes digits/underscores.
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_HAS_DIGIT = re.compile(r"\d")


def validate_password(pw):
    """Return (ok, reason_ar). `pw` may be None or non-string; safe.

    Reason strings are all user-facing Arabic so callers can flash
    them without further translation.
    """
    if not pw or not isinstance(pw, str):
        return False, "كلمة السر مطلوبة."
    if len(pw) < MIN_LENGTH:
        return False, f"كلمة السر يجب ألا تقل عن {MIN_LENGTH} حروف."
    if not _HAS_LETTER.search(pw):
        return False, "كلمة السر يجب أن تحتوي على حرف واحد على الأقل."
    if not _HAS_DIGIT.search(pw):
        return False, "كلمة السر يجب أن تحتوي على رقم واحد على الأقل."
    return True, ""


def hint_text_ar():
    """Short helper text safe to render under any password input."""
    return (f"الحد الأدنى {MIN_LENGTH} حروف، وتحتوي على حرف ورقم على الأقل.")
