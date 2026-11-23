"""MARSOUD-P125 — WhatsApp Integration settings & safe wa.me helper routes."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
from flask_login import login_required, current_user

from app import db
from app.models import WhatsAppIntegration
from app.services.permissions import get_user_role
from app.services.whatsapp_sync import send_cloud_template

bp = Blueprint("settings_whatsapp", __name__, url_prefix="/settings/whatsapp")


@bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    cid = g.active_company.id
    role = get_user_role(current_user.id, cid)
    if role not in ("owner", "admin", "ceo", "sales_manager"):
        abort(403)
    cfg = WhatsAppIntegration.get_or_create(cid)
    if request.method == "POST":
        cc = "".join(ch for ch in (request.form.get("default_country_code") or "966") if ch.isdigit())
        cfg.default_country_code = (cc or "966")[:6]
        cfg.invoice_footer_note = (request.form.get("invoice_footer_note") or "").strip()[:300] or None
        cfg.is_cloud_enabled = request.form.get("is_cloud_enabled") == "1"
        cfg.phone_number_id = (request.form.get("phone_number_id") or "").strip()[:64] or None
        tok = (request.form.get("access_token") or "").strip()
        if tok and tok != "********":
            cfg.access_token = tok
        tpl = (request.form.get("booking_template_name") or "").strip()[:120]
        if tpl:
            cfg.booking_template_name = tpl
        lang = (request.form.get("booking_template_lang") or "ar").strip()[:16]
        cfg.booking_template_lang = lang or "ar"
        db.session.commit()
        flash("تم حفظ إعدادات تكامل واتساب بنجاح.", "success")
        return redirect(url_for("settings_whatsapp.index"))

    return render_template("settings/whatsapp.html", cfg=cfg)


@bp.route("/test", methods=["POST"])
@login_required
def test_send():
    cid = g.active_company.id
    role = get_user_role(current_user.id, cid)
    if role not in ("owner", "admin", "ceo", "sales_manager"):
        abort(403)
    phone = (request.form.get("test_phone") or "").strip()
    if not phone:
        flash("أدخل رقم موبايل للاختبار.", "error")
        return redirect(url_for("settings_whatsapp.index"))
    cfg = WhatsAppIntegration.get_or_create(cid)
    res = send_cloud_template(
        cid,
        phone,
        cfg.booking_template_name,
        lang_code=cfg.booking_template_lang,
        body_params=["عميل تجريبي", "اليوم 05:00 م"],
    )
    if res.get("ok"):
        flash("تم إرسال رسالة الاختبار عبر Meta Cloud API بنجاح.", "success")
    else:
        flash(f"لم يتم الإرسال: {res.get('error') or res.get('skipped')}", "error")
    return redirect(url_for("settings_whatsapp.index"))
