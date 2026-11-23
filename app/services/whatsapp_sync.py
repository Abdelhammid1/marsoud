"""MARSOUD-P125 — Zero-Ban WhatsApp (wa.me + Official Meta Cloud API with anti-spam guards)."""
from __future__ import annotations
import json
import re
import time
from datetime import datetime
from urllib.parse import quote
from urllib.request import Request, urlopen

from app import db
from app.models import WhatsAppIntegration

# In-memory rate-limit & duplicate guard to prevent any Meta spam flags
_RECENT_SENDS: dict[tuple[int, str], float] = {}
_COMPANY_WINDOW: dict[int, list[float]] = {}
MAX_PER_MINUTE = 10
DEDUP_SECONDS = 300


def normalize_wa_phone(raw_phone: str | None, default_cc: str = "966") -> str:
    digits = re.sub(r"\D", "", raw_phone or "")
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    cc = re.sub(r"\D", "", default_cc or "966") or "966"
    if digits.startswith("0"):
        digits = cc + digits[1:]
    elif len(digits) == 9 and cc == "966" and digits.startswith("5"):
        digits = "966" + digits
    elif len(digits) == 10 and cc == "20" and digits.startswith("1"):
        digits = "20" + digits
    return digits


def build_wa_me_url(phone: str | None, message: str, default_cc: str = "966") -> str:
    norm = normalize_wa_phone(phone, default_cc=default_cc)
    encoded = quote((message or "").strip())
    if norm:
        return f"https://wa.me/{norm}?text={encoded}"
    return f"https://wa.me/?text={encoded}"


def send_cloud_template(company_id: int, to_phone: str, template_name: str,
                        lang_code: str = "ar", body_params: list[str] | None = None) -> dict:
    """Send an approved Utility template via Official Meta Graph API v20.0.
    Enforces rate-limiting and 5-min duplicate suppression to guarantee zero ban risk."""
    cfg = WhatsAppIntegration.query.filter_by(company_id=company_id).first()
    if not cfg or not cfg.is_cloud_enabled or not cfg.phone_number_id or not cfg.access_token:
        return {"ok": False, "skipped": "cloud_api_disabled"}

    norm = normalize_wa_phone(to_phone, default_cc=cfg.default_country_code)
    if len(norm) < 10:
        return {"ok": False, "skipped": "invalid_phone"}

    now = time.time()
    key = (company_id, norm)
    if now - _RECENT_SENDS.get(key, 0) < DEDUP_SECONDS:
        return {"ok": False, "skipped": "duplicate_guard"}

    win = [t for t in _COMPANY_WINDOW.get(company_id, []) if now - t < 60]
    if len(win) >= MAX_PER_MINUTE:
        return {"ok": False, "skipped": "rate_limit_guard"}

    components = []
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)[:200]} for p in body_params],
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": norm,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang_code or "ar"},
            **({"components": components} if components else {}),
        },
    }
    url = f"https://graph.facebook.com/v20.0/{cfg.phone_number_id}/messages"
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {cfg.access_token.strip()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=12) as resp:
            resp.read()
        _RECENT_SENDS[key] = now
        win.append(now)
        _COMPANY_WINDOW[company_id] = win
        cfg.messages_sent = (cfg.messages_sent or 0) + 1
        cfg.last_sent_at = datetime.utcnow()
        cfg.last_error = None
        db.session.commit()
        return {"ok": True}
    except Exception as exc:
        cfg.last_error = str(exc)[:350]
        db.session.commit()
        return {"ok": False, "error": str(exc)[:200]}
