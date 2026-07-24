"""MARSOUD-TERMS-CONSENT (Abdelhamid 2026-07-22) — public /terms
and /privacy pages. Content is authored by super-admin at
/admin/legal and served here without a login gate so the register
page can link to them, and so any user can review before accepting.

MARSOUD-PUBLIC-CONTACT-FORM-01 (Abdelhamid 2026-07-24) — public
POST /api/v1/public/contact-lead endpoint that converts Manasty's
website contact form into a Lead. Token-gated, rate-limited,
fail-closed.
"""
import re
import secrets
import threading
import time
from collections import deque
from flask import Blueprint, render_template, request, jsonify, url_for, current_app
from markupsafe import Markup


bp = Blueprint("public", __name__)


# ─── MARSOUD-PUBLIC-CONTACT-FORM-01 rate limiter ─────────────────
# Per-IP sliding window: max 5 requests per 60 s. In-memory only —
# a real Redis limiter would sit in front in prod, but the ticket
# said "lightweight" and this stays within scope.
_CONTACT_WINDOW_SECS = 60
_CONTACT_MAX_PER_WINDOW = 5
_contact_ip_history = {}   # ip → deque[timestamps]
_contact_lock = threading.Lock()


def _rate_limit_ok(ip):
    now = time.monotonic()
    with _contact_lock:
        q = _contact_ip_history.setdefault(ip, deque())
        # Drop entries older than the window.
        while q and (now - q[0]) > _CONTACT_WINDOW_SECS:
            q.popleft()
        if len(q) >= _CONTACT_MAX_PER_WINDOW:
            return False
        q.append(now)
        return True


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@bp.route("/api/v1/public/contact-lead", methods=["POST"])
def contact_lead():
    """MARSOUD-PUBLIC-CONTACT-FORM-01 — website → CRM Lead.

    Contract:
      · Header: X-Contact-Form-Token: <token>  (required)
      · JSON:   {name, email?, phone?, service, message?}
                — name + service always required
                — at least one of email / phone required
      · Returns: 201 {lead_id} on success
                 401 unauthorized token
                 400 bad payload
                 429 rate-limited
                 500 endpoint disabled (no token configured)
    """
    # 1. Fail-closed gate — no CONTACT_FORM_TOKEN configured ⇒ refuse.
    configured = current_app.config.get("CONTACT_FORM_TOKEN") or ""
    if not configured:
        return jsonify({
            "error": "contact form endpoint disabled",
        }), 500
    provided = request.headers.get("X-Contact-Form-Token") or ""
    if not secrets.compare_digest(provided, configured):
        return jsonify({"error": "unauthorized"}), 401

    # 2. Rate limit — cheap per-IP window.
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "?"
    ip = ip.split(",")[0].strip()
    if not _rate_limit_ok(ip):
        return jsonify({"error": "rate limit exceeded"}), 429

    # 3. Parse + validate payload.
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip().lower() or None
    phone = (payload.get("phone") or "").strip() or None
    service = (payload.get("service") or "").strip()
    message = (payload.get("message") or "").strip() or None
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not service:
        return jsonify({"error": "service is required"}), 400
    if not (email or phone):
        return jsonify({
            "error": "at least one of email or phone is required"}), 400
    if email and not _EMAIL_RE.match(email):
        return jsonify({"error": "invalid email format"}), 400

    # 4. Create the Lead in Manasty's CRM.
    from app import db
    from app.models import Lead, LeadStatus
    from app.services.manasty import (
        manasty_id, manasty_default_assignee_id, manasty_owner_ids,
    )
    assignee = manasty_default_assignee_id()
    if not assignee:
        # Manasty exists but has no owner/admin configured. Refuse
        # cleanly rather than raising a NOT NULL constraint at DB.
        return jsonify({
            "error": "manasty inbox not configured"}), 500
    lead = Lead(
        company_id=manasty_id(),
        client_name=name[:150],
        email=email[:200] if email else None,
        # Lead.phone is NOT NULL in schema — placeholder when only
        # email is supplied so the row can persist.
        phone=(phone or "لم يُقدَّم")[:30],
        service_needed=service[:200],
        source="نموذج التواصل - الموقع",
        status=LeadStatus.NEW_LEAD,
        assigned_to_id=assignee,
        notes=message if message else None,
    )
    db.session.add(lead); db.session.commit()

    # 5. Fan out notifications — never block success on delivery.
    try:
        from app.services.opsflow_extras import notify_users
        from app.models import NotificationKind
        notify_users(
            manasty_owner_ids(),
            company_id=manasty_id(),
            kind=NotificationKind.NEW_LEAD.value,
            title="عميل محتمل جديد من الموقع",
            body=f"{name} — {service}",
            link_url=url_for("leads.detail", lead_id=lead.id,
                              _external=False),
        )
    except Exception:
        current_app.logger.exception("contact-lead notify failed")

    try:
        from app.services.email import send_email
        from app.models import User
        subj = f"عميل محتمل جديد: {name}"
        html = (
            f"<p>عميل محتمل جديد جاء من نموذج التواصل على الموقع.</p>"
            f"<ul>"
            f"<li><b>الاسم:</b> {name}</li>"
            f"<li><b>البريد:</b> {email or '—'}</li>"
            f"<li><b>الموبايل:</b> {phone or '—'}</li>"
            f"<li><b>الخدمة:</b> {service}</li>"
            f"</ul>"
            + (f"<p><b>الرسالة:</b><br>{message}</p>" if message else "")
        )
        for uid in manasty_owner_ids():
            u = db.session.get(User, uid)
            if u and u.email:
                send_email(u.email, subj, html)
    except Exception:
        current_app.logger.exception("contact-lead email failed")

    return jsonify({"ok": True, "lead_id": lead.id}), 201


@bp.route("/terms")
def terms():
    from app.services.legal import get_terms_html, get_terms_version
    return render_template(
        "public/legal.html",
        title="الشروط والأحكام",
        version=get_terms_version(),
        content=Markup(get_terms_html()),
    )


@bp.route("/privacy")
def privacy():
    from app.services.legal import get_privacy_html, get_terms_version
    return render_template(
        "public/legal.html",
        title="سياسة الخصوصية",
        version=get_terms_version(),
        content=Markup(get_privacy_html()),
    )
