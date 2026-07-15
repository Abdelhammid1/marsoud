"""MARSOUD-CRM-STATUS-ACTIVITY-SPLIT (Abdelhamid 2026-07-15) —
per-activity outcome catalogue + status-change suggestions.

Design (from the ticket):
  · Every activity type gets its own list of allowed outcomes.
    A CALL outcome ("لم يرد") is meaningless for a MEETING.
  · Some activities suggest a Lead Status change but NEVER apply
    it automatically — the user has to confirm. That's what stops
    the noisy "status flipped 5 times in a minute" pattern (see
    Image #58 on the ticket).

The outcome list is the source of truth for what the JS dropdown
shows and for the report facet at /reports/activity-outcomes (built
in a follow-up if reporting requests it).
"""
from app.models import LeadActivityType, LeadStatus


# ─── Per-type outcome catalogue ────────────────────────────────────
# Empty tuple = no outcome dropdown (e.g. NOTE — a note doesn't have
# a "result" concept). Values are the display labels; they're stored
# verbatim in LeadActivity.outcome so reports can group by them
# cleanly without a join.
OUTCOMES_BY_TYPE = {
    LeadActivityType.CALL: (
        "تم الرد",
        "لم يرد",
        "مشغول",
        "الرقم غير صحيح",
        "طلب التواصل لاحقًا",
        "غير مهتم",
    ),
    LeadActivityType.WHATSAPP: (
        "تم الرد",
        "لم يرد",
        "مقروء بدون رد",
        "الرقم غير صحيح",
        "طلب التواصل لاحقًا",
    ),
    LeadActivityType.EMAIL: (
        "تم الرد",
        "لم يفتح",
        "فتح بدون رد",
        "رد بالرفض",
    ),
    LeadActivityType.MEETING: (
        "تم الاجتماع",
        "تم التأجيل",
        "تم الإلغاء",
        "لم يحضر",
    ),
    LeadActivityType.VISIT: (
        "تم اللقاء",
        "لم يكن موجودًا",
        "تم التأجيل",
        "تم الإلغاء",
    ),
    LeadActivityType.FILE_SENT: (
        "استلم",
        "لم يفتح",
        "رفض",
    ),
    LeadActivityType.QUOTE_SENT: (
        "قيد الدراسة",
        "قبل",
        "رفض",
        "طلب تعديل",
    ),
    LeadActivityType.CONTRACT_SIGNED: (
        "تم التوقيع",
        "لم يتم التوقيع",
        "قيد المراجعة",
    ),
    LeadActivityType.NOTE: (),   # no outcomes
}


# ─── Status suggestion map ────────────────────────────────────────
# When one of these activities is logged with a "positive" outcome,
# the UI suggests (but never applies) the corresponding Lead Status.
# The suggestion is scoped to the outcome list so a "لم يرد" call
# doesn't propose moving the lead to CONTACTED just because it was
# tagged CONTACTED-adjacent.
STATUS_SUGGESTIONS = {
    # activity type → {outcome value → suggested LeadStatus}
    LeadActivityType.CALL: {
        "تم الرد": LeadStatus.CONTACTED,
    },
    LeadActivityType.WHATSAPP: {
        "تم الرد": LeadStatus.CONTACTED,
    },
    LeadActivityType.EMAIL: {
        "تم الرد": LeadStatus.CONTACTED,
    },
    LeadActivityType.MEETING: {
        "تم الاجتماع": LeadStatus.MEETING_SCHEDULED,
    },
    LeadActivityType.VISIT: {
        "تم اللقاء": LeadStatus.MEETING_SCHEDULED,
    },
    LeadActivityType.QUOTE_SENT: {
        "قيد الدراسة": LeadStatus.PROPOSAL_SENT,
        "قبل": LeadStatus.PROPOSAL_SENT,
        "طلب تعديل": LeadStatus.PROPOSAL_SENT,
        "رفض": LeadStatus.LOST,
    },
    LeadActivityType.CONTRACT_SIGNED: {
        "تم التوقيع": LeadStatus.WON,
    },
}


def outcomes_for(activity_type):
    """List of outcome strings the UI should offer for a type.
    Returns () for NOTE (no dropdown)."""
    if isinstance(activity_type, str):
        try:
            activity_type = LeadActivityType[activity_type]
        except KeyError:
            return ()
    return OUTCOMES_BY_TYPE.get(activity_type, ())


def suggest_status(activity_type, outcome, current_status):
    """Return a LeadStatus suggestion or None. The caller renders
    a small "هل ترغب في تحديث الحالة إلى X؟ [نعم] [لا]" panel.

    Rules:
      · Empty outcome → no suggestion (user hasn't recorded a result).
      · Outcome not in the map → no suggestion.
      · Suggested status == current_status → no suggestion (avoid
        prompting for a no-op).
    """
    if not outcome:
        return None
    if isinstance(activity_type, str):
        try:
            activity_type = LeadActivityType[activity_type]
        except KeyError:
            return None
    table = STATUS_SUGGESTIONS.get(activity_type, {})
    suggested = table.get(outcome)
    if suggested is None:
        return None
    if suggested == current_status:
        return None
    return suggested


def all_outcomes_json():
    """Serialize the whole catalogue for the JS dropdown that swaps
    options when the type selector changes on the activity form."""
    return {
        t.name: list(OUTCOMES_BY_TYPE.get(t, ()))
        for t in LeadActivityType
    }
