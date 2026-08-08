from flask import (
    Blueprint, render_template, redirect, url_for, g,
    request, send_from_directory, current_app,
)
from flask_login import login_required, current_user
from app.services.reports import dashboard_metrics
from app.services.invoicing import update_overdue_statuses

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def landing():
    return send_from_directory(current_app.static_folder, "landing.html")


@bp.route("/home")
@login_required
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    update_overdue_statuses(g.active_company.id)
    # MARSOUD-DASH-FILTER — اليوم / الشهر / الربع / السنة
    period = request.args.get("period", "month")
    metrics = dashboard_metrics(g.active_company.id, period=period)
    # MARSOUD-TASK-ARCHIVE-MINE (2026-08-08) — the "🗂 أرشيفي"
    # dashboard tile is per-user, so it lives outside the
    # company-scoped dashboard_metrics(). Computed here so the
    # metrics dict remains a single source of truth for the tile
    # renderer.
    try:
        from app.services.task_archive import my_archived_tasks
        metrics.setdefault("ops", {})["tasks_archived_mine"] = (
            my_archived_tasks(
                g.active_company.id, current_user.id).count())
    except Exception:
        current_app.logger.exception("tasks_archived_mine metric failed")
        metrics.setdefault("ops", {})["tasks_archived_mine"] = 0
    return render_template("dashboard/index.html", metrics=metrics)
