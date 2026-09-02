"""MARSOUD-COST-CENTERS-01 (2026-09-02) — cost centers CRUD.

Owner / admin manages structure (create / edit / toggle active /
delete). Every other role only reads via `cost_centers.view`. Delete
refuses when any JournalLine references the CC — a hard delete would
break the report + the reversal-inheritance guarantee.
"""
from datetime import datetime
from flask import (
    Blueprint, render_template, redirect, url_for, request, g, flash,
    abort,
)
from flask_login import login_required, current_user

from app import db
from app.models import CostCenter, Department, JournalLine
from app.services.permissions import require_permission


bp = Blueprint("cost_centers", __name__)


def _load_or_404(cc_id):
    cc = db.session.get(CostCenter, int(cc_id))
    if not cc or cc.company_id != g.active_company.id \
            or cc.deleted_at is not None:
        abort(404)
    return cc


@bp.route("/")
@login_required
@require_permission("cost_centers.view")
def index():
    cid = g.active_company.id
    show = (request.args.get("show") or "active").strip()
    q = (CostCenter.query
         .filter_by(company_id=cid)
         .filter(CostCenter.deleted_at.is_(None)))
    if show == "active":
        q = q.filter_by(is_active=True)
    elif show == "inactive":
        q = q.filter_by(is_active=False)
    rows = q.order_by(CostCenter.code).all()
    return render_template("cost_centers/index.html",
                            rows=rows, show=show)


@bp.route("/new")
@login_required
@require_permission("cost_centers.manage")
def new():
    cid = g.active_company.id
    departments = (Department.query
                    .filter_by(company_id=cid)
                    .order_by(Department.name).all())
    return render_template("cost_centers/form.html",
                            cc=None, departments=departments)


@bp.route("/", methods=["POST"])
@login_required
@require_permission("cost_centers.manage")
def create():
    cid = g.active_company.id
    code = (request.form.get("code") or "").strip()
    name = (request.form.get("name") or "").strip()
    if not code or not name:
        flash("الكود والاسم مطلوبان", "error")
        return redirect(url_for("cost_centers.new"))
    # Uniqueness — respect the (company_id, code) unique constraint.
    dup = (CostCenter.query
           .filter_by(company_id=cid, code=code)
           .filter(CostCenter.deleted_at.is_(None))
           .first())
    if dup:
        flash("كود مركز التكلفة مستخدم بالفعل", "error")
        return redirect(url_for("cost_centers.new"))
    linked = request.form.get("linked_department_id", type=int)
    cc = CostCenter(
        company_id=cid,
        code=code,
        name=name,
        name_ar=(request.form.get("name_ar") or "").strip() or None,
        description=(request.form.get("description") or "").strip() or None,
        linked_department_id=linked or None,
        is_active=True,
    )
    db.session.add(cc); db.session.commit()
    flash("تم إنشاء مركز التكلفة", "success")
    return redirect(url_for("cost_centers.index"))


@bp.route("/<int:cc_id>/edit")
@login_required
@require_permission("cost_centers.manage")
def edit(cc_id):
    cc = _load_or_404(cc_id)
    cid = g.active_company.id
    departments = (Department.query
                    .filter_by(company_id=cid)
                    .order_by(Department.name).all())
    return render_template("cost_centers/form.html",
                            cc=cc, departments=departments)


@bp.route("/<int:cc_id>", methods=["POST"])
@login_required
@require_permission("cost_centers.manage")
def update(cc_id):
    cc = _load_or_404(cc_id)
    code = (request.form.get("code") or "").strip()
    name = (request.form.get("name") or "").strip()
    if not code or not name:
        flash("الكود والاسم مطلوبان", "error")
        return redirect(url_for("cost_centers.edit", cc_id=cc.id))
    cc.code = code
    cc.name = name
    cc.name_ar = (request.form.get("name_ar") or "").strip() or None
    cc.description = (request.form.get("description") or "").strip() or None
    cc.linked_department_id = (request.form.get(
        "linked_department_id", type=int) or None)
    db.session.commit()
    flash("تم تحديث مركز التكلفة", "success")
    return redirect(url_for("cost_centers.index"))


@bp.route("/<int:cc_id>/toggle-active", methods=["POST"])
@login_required
@require_permission("cost_centers.manage")
def toggle_active(cc_id):
    cc = _load_or_404(cc_id)
    cc.is_active = not cc.is_active
    db.session.commit()
    flash("تم تحديث الحالة", "success")
    return redirect(url_for("cost_centers.index"))


@bp.route("/<int:cc_id>/delete", methods=["POST"])
@login_required
@require_permission("cost_centers.manage")
def delete(cc_id):
    cc = _load_or_404(cc_id)
    # Refuse if any JournalLine references this CC — deleting would
    # break the report + the reversal-inheritance guarantee.
    referenced = (JournalLine.query
                    .filter_by(cost_center_id=cc.id)
                    .first() is not None)
    if referenced:
        flash("لا يمكن حذف مركز تكلفة عليه قيود — يمكن تعطيله بدلاً من ذلك",
              "error")
        return redirect(url_for("cost_centers.index"))
    cc.deleted_at = datetime.utcnow()
    db.session.commit()
    flash("تم حذف مركز التكلفة", "success")
    return redirect(url_for("cost_centers.index"))
