"""MARSOUD-TERMS-CONSENT (Abdelhamid 2026-07-22) — public /terms
and /privacy pages. Content is authored by super-admin at
/admin/legal and served here without a login gate so the register
page can link to them, and so any user can review before accepting.

MARSOUD-PUBLIC-CONTACT-FORM-01 (Abdelhamid 2026-07-24) — public
POST /api/v1/public/contact-lead endpoint that converts Manasty's
website contact form into a Lead. Token-gated, rate-limited,
fail-closed.

MARSOUD-CONTACT-LEAD-01 (Abdelhamid 2026-09-03) — endpoint extended
so the two landing forms (index.html #contact + contact.html) can
send structured fields (company_name, package, description) and a
per-form source tag (landing_form | contact_page). Adds a 2-minute
DB-backed idempotency check on (phone, description) so double-click
Submit / retry-after-page-nav never creates dup Lead rows. Old
payload shape (name/email/phone/service/message) still accepted so
cached browser copies keep working through the deploy window.
"""
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
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

# MARSOUD-CONTACT-LEAD-01 — source whitelist. Rejecting arbitrary
# strings keeps the CRM's source filter tidy and stops a caller who
# guesses the token from injecting garbage into sales dashboards.
_SOURCE_LABELS = {
    "landing_form": "نموذج الصفحة الرئيسية",
    "contact_page": "صفحة التواصل",
    # Default matches the pre-MARSOUD-CONTACT-LEAD-01 hardcode so
    # historical rows and new default-shape rows share one bucket.
    "website":      "نموذج التواصل - الموقع",
}
_DEDUP_WINDOW = timedelta(minutes=2)
_PHONE_PLACEHOLDER = "لم يُقدَّم"


def _build_notes(company_name, package, description):
    """Format the free-text `Lead.notes` block. `description` goes
    into `Lead.request_description`; this block carries the two
    extras (company + package) so sales can read them at a glance
    even before opening the full detail view. Returns None when
    both extras are empty (keeps the column NULL rather than an
    empty string)."""
    parts = []
    if company_name:
        parts.append(f"الشركة: {company_name}")
    if package:
        parts.append(f"الباقة: {package}")
    if not parts:
        return None
    return "\n".join(parts)


@bp.route("/api/v1/public/contact-lead", methods=["POST"])
def contact_lead():
    """MARSOUD-PUBLIC-CONTACT-FORM-01 + MARSOUD-CONTACT-LEAD-01 —
    Manasty landing forms → CRM Lead.

    Contract (accepts both shapes for backward compat):
      · Header: X-Contact-Form-Token: <token>  (required)
      · New shape:  {name, phone, company_name?, service_type,
                     package?, description?, source?}
      · Old shape:  {name, email?, phone?, service, message?}
      · Normalisation is service = service_type|service, description
        = description|message, so any combination works.
      · `source` (new shape) must be one of landing_form,
        contact_page, website. Missing = "website".
      · Returns: 201 {ok, lead_id}                — new Lead created
                 200 {ok, lead_id, dedup: true}   — matched an existing
                                                    Lead within 2 min
                 401 unauthorized token
                 400 bad payload / bad source
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

    # 3. Parse + validate payload — accept BOTH old and new shapes.
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    email = (payload.get("email") or "").strip().lower() or None
    phone = (payload.get("phone") or "").strip() or None
    # `service_type` = new shape; `service` = old shape.
    service = (payload.get("service_type") or payload.get("service")
                or "").strip()
    # `description` = new shape; `message` = old shape.
    description = (payload.get("description") or payload.get("message")
                    or "").strip() or None
    company_name = (payload.get("company_name") or "").strip() or None
    package = (payload.get("package") or "").strip() or None
    source_key = (payload.get("source") or "website").strip().lower()

    if not name:
        return jsonify({"error": "name is required"}), 400
    if not service:
        return jsonify({"error": "service is required"}), 400
    if not (email or phone):
        return jsonify({
            "error": "at least one of email or phone is required"}), 400
    if email and not _EMAIL_RE.match(email):
        return jsonify({"error": "invalid email format"}), 400
    if source_key not in _SOURCE_LABELS:
        return jsonify({
            "error": (f"invalid source: must be one of "
                       f"{sorted(_SOURCE_LABELS)}")}), 400

    # 4. Resolve Manasty + owner assignee before touching the DB.
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

    normalised_phone = (phone or _PHONE_PLACEHOLDER)[:30]
    company_id = manasty_id()

    # 5. Idempotency — swallow duplicate submits from the same
    #    caller within a 2-minute window. Only meaningful when we
    #    have a real phone; email-only leads skip this (their
    #    phone falls to the placeholder and would false-collide
    #    across unrelated visitors).
    if phone and normalised_phone != _PHONE_PLACEHOLDER:
        cutoff = datetime.utcnow() - _DEDUP_WINDOW
        q = Lead.query.filter(
            Lead.company_id == company_id,
            Lead.phone == normalised_phone,
            Lead.created_at >= cutoff,
            Lead.deleted_at.is_(None),
        )
        # When we have a description prefer matching on it (people
        # rarely retype the exact same free text on purpose). Otherwise
        # fall back to service — a phone hitting the same service twice
        # in 2 minutes is a retry, not a genuine second enquiry.
        if description:
            q = q.filter(Lead.request_description == description)
        else:
            q = q.filter(Lead.service_needed == service[:200])
        existing = q.order_by(Lead.created_at.desc()).first()
        if existing:
            return jsonify({
                "ok": True, "lead_id": existing.id, "dedup": True,
            }), 200

    # 6. Create the Lead.
    lead = Lead(
        company_id=company_id,
        client_name=name[:150],
        email=email[:200] if email else None,
        # Lead.phone is NOT NULL in schema — placeholder when only
        # email is supplied so the row can persist.
        phone=normalised_phone,
        service_needed=service[:200],
        source=_SOURCE_LABELS[source_key][:100],
        status=LeadStatus.NEW_LEAD,
        assigned_to_id=assignee,
        request_description=description,
        notes=_build_notes(company_name, package, description),
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
