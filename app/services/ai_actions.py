"""MARSOUD-AI-ACTION-FRAMEWORK-01 (2026-09-03) — Confirm-to-Execute
service layer.

Public API:
  * `register_action(action_type, spec)`      — Phase 1+ tickets call
    this at import time to plug an action into the registry.
  * `propose(action_type, payload, ...)`      — validate + persist a
    PENDING row + return the confirmation card.
  * `confirm(intent_id, ...)`                 — check freshness, run
    the executor via the SAME service functions the visible UI uses,
    return `(result, http_status)`.

No action other than `test_echo` ships in this ticket. Every real
action_type (add_lead, add_vendor_bill, create_payroll_run, …) is a
follow-up that registers itself here without touching the framework.
"""
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from flask import current_app

from app import db
from app.models import (
    AiActionIntent, AiActionIntentStatus, DEFAULT_EXPIRY_MINUTES,
)
from app.services.agent_safety import _canonical_args


_logger = logging.getLogger("marsoud.ai_actions")


class AiActionError(Exception):
    """User-facing validation / registry failure."""


@dataclass(frozen=True)
class ActionSpec:
    """One entry in the action registry. Every real action_type
    registers a spec at import time — the framework never learns
    about specific actions, only how to dispatch them.

    validate    : callable(payload)   raises AiActionError on bad input.
    fingerprint : callable(payload, company_id) → str.
                  Called TWICE — at propose time (against the frozen
                  payload) and at confirm time. When the two disagree
                  the row goes STALE. Real Phase-1 actions read from
                  the DB inside fingerprint so a vendor rename
                  between propose and confirm surfaces cleanly.
    execute     : callable(payload, company_id, actor_user_id) → dict.
                  Must call the SAME service function the visible
                  form uses. Return dict is JSON-serialisable and
                  goes into `result_json`.
    describe_ar : callable(payload) → list[(label_ar, value_ar)] —
                  drives the confirmation card the chat renders.
    required_perm : optional permission code; propose refuses when
                    the caller doesn't have it.
    """
    validate: Callable[[dict], None]
    fingerprint: Callable[[dict, int], str]
    execute: Callable[[dict, int, int], dict]
    describe_ar: Callable[[dict], list]
    required_perm: Optional[str] = None


_ACTION_REGISTRY: dict[str, ActionSpec] = {}


def register_action(action_type: str, spec: ActionSpec) -> None:
    if not action_type or not isinstance(action_type, str):
        raise ValueError("action_type must be a non-empty str")
    if action_type in _ACTION_REGISTRY:
        # Refuse silent overwrites — a Phase-2 ticket adding an
        # action_type that collides with a Phase-1 one is a bug we
        # want to hear about at boot, not paper over.
        raise ValueError(
            f"action_type {action_type!r} already registered")
    _ACTION_REGISTRY[action_type] = spec


def registered_action_types() -> list:
    return sorted(_ACTION_REGISTRY.keys())


# ─── propose ────────────────────────────────────────────────────────
def propose(*, action_type, payload, company_id, user_id):
    """Persist a PENDING AiActionIntent. Returns the confirmation
    card the chat renders. Raises AiActionError on bad input.
    """
    spec = _ACTION_REGISTRY.get(action_type)
    if spec is None:
        raise AiActionError(
            f"نوع العملية غير معروف: {action_type}")

    # Permission check (framework-level; the visible UI still has
    # its own gate).
    if spec.required_perm:
        from app.services.permissions import has_permission
        if not has_permission(spec.required_perm):
            raise AiActionError(
                "ليس لديك صلاحية اقتراح هذا النوع من العمليات")

    if not isinstance(payload, dict):
        raise AiActionError("payload لازم يكون object")

    spec.validate(payload)   # may raise AiActionError

    canonical = _canonical_args(payload)
    fp = spec.fingerprint(payload, company_id)
    if not isinstance(fp, str) or not fp:
        # A misbehaving action returning None would silently bypass
        # the stale check — refuse loudly. Length is not policed;
        # the sha256 below normalises to 64 chars anyway.
        raise AiActionError(
            "خطأ داخلي: بصمة العملية غير صالحة")
    payload_hash = _sha256(fp)

    now = datetime.utcnow()
    row = AiActionIntent(
        company_id=company_id,
        action_type=action_type,
        payload=canonical,
        payload_hash=payload_hash,
        status=AiActionIntentStatus.PENDING,
        proposed_by="ai_agent",
        proposed_by_user_id=user_id,
        created_at=now,
        expires_at=now + timedelta(minutes=DEFAULT_EXPIRY_MINUTES),
    )
    db.session.add(row); db.session.commit()

    card = _card_from_describe(spec.describe_ar(payload))
    return {
        "intent_id": row.id,
        "action_type": action_type,
        "expires_at": row.expires_at.isoformat(),
        "expires_in_seconds":
            int((row.expires_at - now).total_seconds()),
        "confirmation_card": card,
    }


# ─── confirm ────────────────────────────────────────────────────────
def confirm(*, intent_id, actor_user_id, active_company_id):
    """Run the executor for a PENDING intent. Returns
    `(result_dict, http_status)`. `http_status`:
      · 200 on success
      · 404 unknown intent / cross-tenant
      · 409 already-consumed / stale
      · 410 expired
      · 500 executor raised (row → REJECTED)
    """
    row = db.session.get(AiActionIntent, intent_id)
    if row is None:
        return {"error": "الاقتراح غير موجود"}, 404
    if row.company_id != active_company_id:
        # Same "hidden vs forbidden" discipline as HR03 /
        # employee_documents — refuse with 404, log the attempt.
        _logger.warning(
            "ai_actions: cross-tenant confirm attempt "
            "intent=%s actor=%s active=%s owner=%s",
            row.id, actor_user_id, active_company_id, row.company_id)
        return {"error": "الاقتراح غير موجود"}, 404

    if row.status != AiActionIntentStatus.PENDING:
        # Idempotency: a second "confirm" click on the same row
        # doesn't re-run anything.
        return {
            "error": ("الاقتراح لا يمكن تنفيذه في حالته الحالية: "
                       + row.status.label_ar),
            "status": row.status.value,
        }, 409

    now = datetime.utcnow()
    if now >= row.expires_at:
        row.status = AiActionIntentStatus.EXPIRED
        db.session.commit()
        return {
            "error": "انتهت صلاحية الاقتراح — جهزلي اقتراح جديد.",
            "status": "EXPIRED",
        }, 410

    spec = _ACTION_REGISTRY.get(row.action_type)
    if spec is None:
        # An action_type was registered when propose ran but the
        # deploy has since removed it. Refuse.
        row.status = AiActionIntentStatus.REJECTED
        row.reject_reason = (
            f"نوع العملية {row.action_type} لم يعد مسجَّلاً")
        db.session.commit()
        return {"error": row.reject_reason}, 409

    # Transient CONFIRMED marker — visible in the UI as
    # "قيد التنفيذ" if the executor is slow.
    row.status = AiActionIntentStatus.CONFIRMED
    row.confirmed_at = now
    row.confirmed_by_user_id = actor_user_id
    db.session.flush()

    # Freshness recheck: recompute the fingerprint (which for real
    # actions consults DB state) and compare.
    try:
        payload = json.loads(row.payload)
    except (TypeError, ValueError):
        payload = {}
    fresh_hash = _sha256(spec.fingerprint(payload, row.company_id))
    if fresh_hash != row.payload_hash:
        row.status = AiActionIntentStatus.STALE
        row.reject_reason = (
            "البيانات اتغيرت بعد الاقتراح — جهزلي اقتراح جديد.")
        db.session.commit()
        return {
            "error": row.reject_reason,
            "status": "STALE",
        }, 409

    # Run the executor. Any exception → rollback the executor's own
    # session writes, mark REJECTED with the message, propagate the
    # HTTP 500 so the caller sees the failure. The intent row itself
    # survives thanks to the second commit after rollback.
    try:
        result = spec.execute(payload, row.company_id, actor_user_id)
    except Exception as e:   # noqa: BLE001
        db.session.rollback()
        # After rollback, re-load the row and mark it REJECTED. Two
        # commits here (one for the executor's rollback, one for the
        # REJECTED status) is the accepted pattern in the codebase
        # for guaranteeing the audit trail even when the write path
        # fails.
        _logger.exception(
            "ai_actions: executor failed for intent=%s", intent_id)
        stale = db.session.get(AiActionIntent, intent_id)
        if stale is not None:
            stale.status = AiActionIntentStatus.REJECTED
            stale.reject_reason = str(e)[:2000]
            db.session.commit()
        return {
            "error": str(e), "status": "REJECTED",
        }, 500

    row.status = AiActionIntentStatus.EXECUTED
    row.executed_at = datetime.utcnow()
    try:
        row.result_json = json.dumps(
            result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        row.result_json = None
    db.session.commit()
    return {
        "ok": True, "intent_id": row.id,
        "status": "EXECUTED", "result": result,
    }, 200


# ─── helpers ────────────────────────────────────────────────────────
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _card_from_describe(pairs):
    """Normalise describe_ar output. Accepts [(k, v)] tuples or
    [{"label": k, "value": v}] dicts; returns a uniform list of
    dicts so the chat template doesn't branch."""
    out = []
    for item in (pairs or []):
        if isinstance(item, dict):
            out.append({"label": str(item.get("label", "")),
                         "value": str(item.get("value", ""))})
        else:
            k, v = item
            out.append({"label": str(k), "value": str(v)})
    return out


# ═══════════════════════════════════════════════════════════════════
# test_echo — the only action_type shipped in this foundation
# ticket. Proof-of-concept for the pipe with zero side-effect on
# money / stock / rows. Phase-1 tickets add real actions.
# ═══════════════════════════════════════════════════════════════════
def _echo_validate(payload):
    msg = payload.get("message")
    if not isinstance(msg, str) or not msg.strip():
        raise AiActionError("message مطلوب ولازم يكون نص غير فارغ")
    if len(msg) > 500:
        raise AiActionError("الرسالة طويلة جدًا (الحد 500 حرف)")


def _echo_fingerprint(payload, _company_id):
    # test_echo has no server-side data source; fingerprint is
    # derived purely from the payload, so STALE only fires if the
    # canonicalised payload_hash was tampered with.
    return f"echo|{payload.get('message', '')}"


def _echo_execute(payload, _company_id, _actor_user_id):
    msg = payload["message"]
    return {"echoed": msg, "length": len(msg)}


def _echo_describe(payload):
    msg = payload.get("message", "")
    return [
        ("النوع", "اختبار الصدى (test_echo)"),
        ("الرسالة", msg),
        ("الطول", str(len(msg))),
    ]


register_action("test_echo", ActionSpec(
    validate=_echo_validate,
    fingerprint=_echo_fingerprint,
    execute=_echo_execute,
    describe_ar=_echo_describe,
    required_perm=None,
))
