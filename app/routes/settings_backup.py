"""MARSOUD — owner-only "download the whole company as Excel" page.

Abdelhamid wants a complete xlsx of every company-scoped table before
he rebuilds the chart of accounts, so he can manually re-key from the
Excel into the new tree if anything goes sideways.
"""
from flask import (
    Blueprint, render_template, redirect, url_for, flash, send_file, g,
)
from flask_login import login_required, current_user

from app import db
from app.models.user import user_companies
from app.services.company_backup import (
    build_company_workbook, workbook_filename,
)

bp = Blueprint("settings_backup", __name__)


def _is_owner_of_active_company():
    if not g.get("active_company"):
        return False
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == current_user.id) &
            (user_companies.c.company_id == g.active_company.id) &
            (user_companies.c.role == "owner"),
        )
    ).first()
    return row is not None


@bp.route("/")
@login_required
def index():
    if not _is_owner_of_active_company():
        flash("هذه الصفحة للمالك فقط", "error")
        return redirect(url_for("dashboard.index"))
    return render_template("settings/backup.html",
                            company=g.active_company)


@bp.route("/excel", methods=["POST"])
@login_required
def download_excel():
    if not _is_owner_of_active_company():
        flash("هذه الصفحة للمالك فقط", "error")
        return redirect(url_for("dashboard.index"))
    try:
        buf = build_company_workbook(g.active_company.id)
    except Exception as e:  # noqa: BLE001
        flash(f"تعذّر إنشاء النسخة الاحتياطية: {e}", "error")
        return redirect(url_for("settings_backup.index"))
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=workbook_filename(g.active_company),
    )
