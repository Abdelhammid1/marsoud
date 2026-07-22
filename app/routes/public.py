"""MARSOUD-TERMS-CONSENT (Abdelhamid 2026-07-22) — public /terms
and /privacy pages. Content is authored by super-admin at
/admin/legal and served here without a login gate so the register
page can link to them, and so any user can review before accepting.
"""
from flask import Blueprint, render_template
from markupsafe import Markup


bp = Blueprint("public", __name__)


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
