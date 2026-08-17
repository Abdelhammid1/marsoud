"""MARSOUD-APPROVAL-GATED-SUPERADMIN (2026-08-12) — approval
gate + queue + replay executor for restricted superadmins.

The design in one paragraph: a superadmin with
`requires_approval=True` can browse `superadmin.*` (GET
passes through) but every POST/PUT/DELETE is intercepted at
the shared @superadmin_required decorator and routed through
`gate_request()` in this file. Destructive endpoints listed
in `DESTRUCTIVE_ENDPOINTS` get queued as
`PendingSuperadminAction` rows and the actor is redirected
to /admin/pending-actions with a flash. Anything else fails
safe with 403 — a new route added to superadmin.py without
being registered here is REFUSED, not silently executed.
The primary superadmin (`requires_approval=False`) approves
via `execute_pending()`, which replays the original request
in a `test_request_context` after setting
`g._approval_bypass=True` so the gate stays out of the way.

Registry review = ticket audit trail. Every future
superadmin write MUST be added to `DESTRUCTIVE_ENDPOINTS` or
it's dead-on-arrival for restricted users. This is the
"لازم زياد يعمل قايمة صريحة" invariant from the ticket.
"""
from datetime import datetime
import json
import os
import uuid
from pathlib import Path

from flask import (
    abort, current_app, flash, g, redirect, request, url_for,
)
from flask_login import current_user, login_user
from werkzeug.datastructures import FileStorage, MultiDict

from app import db


class ApprovalError(Exception):
    """Raised by execute_pending / reject_pending on a stale or
    already-decided row. Routes catch this and surface the
    message via flash."""


# ── registry ─────────────────────────────────────────────── #
# 38 destructive endpoints. Any POST/PUT/DELETE on one of
# these from a `requires_approval=True` user is intercepted
# + queued. Every future superadmin write MUST be added
# here or the fail-safe (below) refuses it with 403.
DESTRUCTIVE_ENDPOINTS = {
    # Companies
    "superadmin.company_toggle",
    "superadmin.company_edit",
    "superadmin.company_delete",
    "superadmin.company_restore",
    "superadmin.company_link_user",
    "superadmin.companies_assign_plan",
    "superadmin.view_as",
    # Users
    "superadmin.user_toggle",
    "superadmin.user_reset_password",
    "superadmin.user_unlink",
    "superadmin.user_resend_invite",
    # MARSOUD-RESTRICTED-SUPERADMIN-CREATE-UI (2026-08-13) —
    # a restricted user must not silently create MORE
    # restricted users. The view body also 403s them, but
    # the registry entry is defense in depth so a future
    # copy-paste that drops the view guard still holds.
    "superadmin.user_create_restricted",
    # AI + Ops
    "superadmin.ai_control",
    "superadmin.ai_settings",
    "superadmin.email_test",
    "superadmin.cron_tick_now",
    # Plans
    "superadmin.plans_save",
    "superadmin.plans_delete",
    # Subscriptions
    "superadmin.subscriptions_renew",
    "superadmin.subscription_settings",
    # Broadcasts
    "superadmin.broadcasts_new",
    "superadmin.broadcasts_send",
    # Coupons
    "superadmin.coupons_new",
    "superadmin.coupons_toggle",
    # Feature flags + overrides
    "superadmin.feature_flags_index",
    "superadmin.overrides_index",
    "superadmin.overrides_revoke",
    # Quotas + Legal
    "superadmin.quotas_save",
    "superadmin.legal",
    # Help center
    "superadmin.help_new",
    "superadmin.help_edit",
    "superadmin.help_toggle",
    "superadmin.help_delete",
    "superadmin.help_add_example",
    "superadmin.help_delete_example",
    "superadmin.help_add_media",     # + file upload staging
    "superadmin.help_delete_media",
    # SaaS billing
    "superadmin.saas_mark_paid",
    "superadmin.saas_price_lock",
}

# Endpoints that a restricted superadmin can POST to even
# without approval. `view_as_stop` exists so a mid-
# impersonation user can always exit; today it also
# happens to be the only superadmin route without
# @superadmin_required so the gate wouldn't reach it, but
# keeping it here documents the exemption.
SELF_SCOPED_EXEMPT = {
    "superadmin.view_as_stop",
    # The approval inbox itself + its decide endpoint —
    # never intercept them (a restricted user is 403'd by
    # the view body anyway, but redirecting a POST here
    # would loop).
    "superadmin.pending_actions",
    "superadmin.pending_actions_decide",
}

# Human labels for the endpoint codes — used in the
# pending-actions template.
ENDPOINT_LABELS_AR = {
    "superadmin.company_toggle": "تغيير حالة شركة",
    "superadmin.company_edit": "تعديل بيانات شركة",
    "superadmin.company_delete": "حذف شركة",
    "superadmin.company_restore": "استرجاع شركة",
    "superadmin.company_link_user": "ربط مستخدم بشركة",
    "superadmin.companies_assign_plan": "تعيين باقة لشركة",
    "superadmin.view_as": "الدخول كشركة (view-as)",
    "superadmin.user_toggle": "تفعيل/تعطيل مستخدم",
    "superadmin.user_reset_password": "إعادة تعيين كلمة المرور",
    "superadmin.user_unlink": "فك ربط مستخدم من شركة",
    "superadmin.user_resend_invite": "إعادة إرسال دعوة",
    "superadmin.user_create_restricted":
        "إنشاء / ترقية مسؤول مقيَّد",
    "superadmin.ai_control": "تحكم في الذكاء الاصطناعي",
    "superadmin.ai_settings": "إعدادات الذكاء الاصطناعي",
    "superadmin.email_test": "اختبار إرسال بريد",
    "superadmin.cron_tick_now": "تشغيل مهمة مجدولة الآن",
    "superadmin.plans_save": "حفظ باقة",
    "superadmin.plans_delete": "حذف باقة",
    "superadmin.subscriptions_renew": "تجديد اشتراك",
    "superadmin.subscription_settings": "إعدادات الاشتراكات",
    "superadmin.broadcasts_new": "إنشاء إعلان",
    "superadmin.broadcasts_send": "إرسال إعلان",
    "superadmin.coupons_new": "إنشاء كوبون",
    "superadmin.coupons_toggle": "تفعيل/تعطيل كوبون",
    "superadmin.feature_flags_index": "تعديل مفاتيح المزايا",
    "superadmin.overrides_index": "منح صلاحية استثنائية",
    "superadmin.overrides_revoke": "إلغاء صلاحية استثنائية",
    "superadmin.quotas_save": "حفظ حصص باقة",
    "superadmin.legal": "نشر إصدار قانوني",
    "superadmin.help_new": "إضافة مقال دعم",
    "superadmin.help_edit": "تعديل مقال دعم",
    "superadmin.help_toggle": "تفعيل/تعطيل مقال",
    "superadmin.help_delete": "حذف مقال",
    "superadmin.help_add_example": "إضافة مثال",
    "superadmin.help_delete_example": "حذف مثال",
    "superadmin.help_add_media": "رفع وسائط",
    "superadmin.help_delete_media": "حذف وسائط",
    "superadmin.saas_mark_paid": "تعليم فاتورة SaaS كمدفوعة",
    "superadmin.saas_price_lock": "تثبيت سعر باقة لشركة",
}


# ── gate ─────────────────────────────────────────────────── #
def gate_request():
    """The choke point. Called by @superadmin_required ONLY
    when the caller has `is_superadmin=True` AND
    `requires_approval=True`. Returns:
      · None → let the request fall through to the view
        (GET/HEAD/OPTIONS, self-scoped exempt, or an
        in-flight approval bypass).
      · a Flask response → intercept the request and return
        this instead (302 redirect after queueing, or 403).
    """
    # In-flight approval execution — the primary is
    # replaying a queued action right now. Let it through.
    if g.get("_approval_bypass"):
        return None
    # Q1 sign-off: GET/HEAD/OPTIONS always pass so the
    # restricted user can browse forms and read data.
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    ep = request.endpoint or ""
    if ep in SELF_SCOPED_EXEMPT:
        return None
    if ep in DESTRUCTIVE_ENDPOINTS:
        _queue_pending_action(request)
        flash("تم إرسال الطلب — في انتظار موافقة المسؤول",
              "info")
        return redirect(url_for("superadmin.pending_actions"))
    # Fail-safe: unregistered POST/PUT/DELETE on
    # superadmin.* → refuse. A new destructive route added
    # to superadmin.py without being registered above is
    # DEAD-ON-ARRIVAL for restricted users. This is the
    # invariant the ticket demands ("لازم يترفض تلقائياً").
    abort(403)


def _staging_dir():
    """Filesystem home for parked uploads. Created lazily."""
    d = (Path(current_app.root_path) / "static"
         / "staging" / "pending_actions")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _queue_pending_action(req):
    """Persist the incoming request as a pending row. Uploads
    are parked under static/staging/pending_actions/ with UUID
    prefixes; the staging map is stored as JSON."""
    from app.models import PendingSuperadminAction
    from app.services.superadmin import log_platform_action

    # request.form is a MultiDict — {k: [values]} preserves
    # multi-value fields (e.g. modules[]).
    form_data = {k: req.form.getlist(k) for k in req.form.keys()}

    staged = {}
    if req.files:
        sd = _staging_dir()
        for field, fs in req.files.items(multi=True):
            if not fs or not fs.filename:
                continue
            safe = f"{uuid.uuid4().hex}_{fs.filename}"
            disk = sd / safe
            fs.save(str(disk))
            # Multiple files under one field name → last wins;
            # this matches the current help_add_media UX (one
            # file per submit). If a future endpoint sends
            # arrays, extend this to a list-per-field.
            staged[field] = str(disk)

    row = PendingSuperadminAction(
        actor_id=current_user.id,
        endpoint=req.endpoint,
        method=req.method,
        url_path=req.full_path,
        view_args=json.dumps(req.view_args or {}),
        form_data=json.dumps(form_data),
        staged_files=json.dumps(staged) if staged else None,
    )
    db.session.add(row)
    db.session.commit()

    log_platform_action(
        "superadmin_action_queued",
        actor_id=current_user.id,
        details=f"endpoint={req.endpoint} row=#{row.id}",
    )
    return row


# ── executor ─────────────────────────────────────────────── #
def execute_pending(action_id, approver_id, note=None):
    """Replay a queued action inside a synthesized request
    context. Sets `g._approval_bypass=True` so the gate
    doesn't re-intercept. Returns the view's return value.

    Audit attribution: the row's `actor_id` records the
    original requester; `decided_by` records the approver.
    Inside the replay, `current_user` is the approver so
    any log_platform_action() call the view makes internally
    attributes to the approver. That's the design — the
    approver is answering with their own name.
    """
    from app.models import PendingSuperadminAction, User
    from app.services.superadmin import log_platform_action

    action = db.session.get(PendingSuperadminAction, action_id)
    if not action or not action.is_pending:
        raise ApprovalError("الإجراء غير موجود أو تمت معالجته بالفعل")

    view_args = json.loads(action.view_args or "{}")
    form_dict = json.loads(action.form_data or "{}")
    staged = json.loads(action.staged_files or "{}")

    # Build a MultiDict that mirrors what request.form
    # would have been at queue time.
    form_md = MultiDict()
    for k, values in form_dict.items():
        for v in values:
            form_md.add(k, v)

    files_md = MultiDict()
    open_handles = []
    for field, path in staged.items():
        # Strip the "<uuid>_" prefix to recover the original
        # filename the view might inspect.
        base = os.path.basename(path)
        original = base.split("_", 1)[1] if "_" in base else base
        fh = open(path, "rb")
        open_handles.append(fh)
        files_md.add(field, FileStorage(
            stream=fh, filename=original,
        ))

    try:
        # test_request_context accepts an EnvironBuilder-style
        # `data` payload — merge form fields + file streams
        # into one dict.
        data = {}
        for k in form_md.keys():
            values = form_md.getlist(k)
            data[k] = values if len(values) > 1 else values[0]
        for field in files_md.keys():
            data[field] = files_md[field]

        with current_app.test_request_context(
                path=action.url_path,
                method=action.method,
                data=data,
        ):
            approver = db.session.get(User, approver_id)
            login_user(approver)
            g._approval_bypass = True
            view = current_app.view_functions[action.endpoint]
            result = view(**view_args)
    finally:
        for fh in open_handles:
            try:
                fh.close()
            except Exception:
                pass

    action.status = "approved"
    action.decided_by = approver_id
    action.decided_at = datetime.utcnow()
    if note:
        action.decision_note = note
    db.session.commit()

    # Best-effort staging cleanup.
    for path in staged.values():
        try:
            os.remove(path)
        except OSError:
            pass

    log_platform_action(
        "superadmin_action_approved",
        actor_id=approver_id,
        details=(f"row=#{action_id} endpoint={action.endpoint} "
                 f"original_actor={action.actor_id}"),
    )
    return result


def reject_pending(action_id, approver_id, note=None):
    """Mark a queued row rejected. No side-effect is
    applied. Staged files are removed."""
    from app.models import PendingSuperadminAction
    from app.services.superadmin import log_platform_action

    action = db.session.get(PendingSuperadminAction, action_id)
    if not action or not action.is_pending:
        raise ApprovalError("الإجراء غير موجود أو تمت معالجته")

    action.status = "rejected"
    action.decided_by = approver_id
    action.decided_at = datetime.utcnow()
    if note:
        action.decision_note = note

    for path in json.loads(action.staged_files or "{}").values():
        try:
            os.remove(path)
        except OSError:
            pass

    db.session.commit()
    log_platform_action(
        "superadmin_action_rejected",
        actor_id=approver_id,
        details=(f"row=#{action_id} endpoint={action.endpoint} "
                 f"original_actor={action.actor_id}"),
    )


def pending_count():
    """Cheap count for the nav badge."""
    from app.models import PendingSuperadminAction
    return (PendingSuperadminAction.query
            .filter_by(status="pending").count())
