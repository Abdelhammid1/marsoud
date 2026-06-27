import os
from werkzeug.utils import secure_filename
from flask import Blueprint, render_template, redirect, url_for, flash, request, session, g, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Company
from app.models.user import user_companies
from app.models.payroll import Employee
from app.services.seed_coa import seed_default_coa
from app.services.permissions import require_permission

bp = Blueprint("companies", __name__)


# MARSOUD-23 — per-company logo upload
ALLOWED_LOGO_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB


def _save_logo(company, file_storage):
    """Persist an uploaded logo to /static/logos/<company_id>.<ext> and update
    company.logo_path to a `/static/...` URL the browser + email clients can hit.

    Returns the new path on success, or None on validation failure (flashes errors).
    """
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(file_storage.filename)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_LOGO_EXTS:
        flash("صيغة اللوجو غير مدعومة. استخدم: png / jpg / jpeg / gif / webp / svg", "error")
        return None
    # Size sniff — werkzeug exposes content_length but it's optional; read+rewind.
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > MAX_LOGO_BYTES:
        flash("حجم اللوجو يتجاوز 2 ميجا — اختصره وأعد المحاولة", "error")
        return None

    logos_dir = os.path.join(current_app.root_path, "static", "logos")
    os.makedirs(logos_dir, exist_ok=True)
    out_name = f"{company.id}.{ext}"
    out_path = os.path.join(logos_dir, out_name)
    file_storage.save(out_path)
    # Clean up any old logo with a different extension (so we don't accumulate stale copies)
    for old_ext in ALLOWED_LOGO_EXTS:
        if old_ext == ext:
            continue
        stale = os.path.join(logos_dir, f"{company.id}.{old_ext}")
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except OSError:
                pass
    return f"/static/logos/{out_name}"


@bp.route("/")
@login_required
def index():
    from sqlalchemy import func
    from app.models.journal import JournalEntry

    sort = request.args.get("sort", "")
    companies = list(current_user.companies)

    if sort == "activity":
        try:
            rows = db.session.query(
                JournalEntry.company_id,
                func.max(JournalEntry.created_at)
            ).group_by(JournalEntry.company_id).all()
            last_act = {cid: ts for cid, ts in rows}
        except Exception:
            current_app.logger.exception("activity sort fallback")
            last_act = {}
        companies.sort(
            key=lambda c: (last_act.get(c.id) is not None, last_act.get(c.id)),
            reverse=True,
        )
    elif sort == "created_asc":
        from datetime import datetime
        companies.sort(key=lambda c: c.created_at or datetime.min)

    return render_template("companies/index.html", companies=companies, sort=sort)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        base_currency = request.form.get("base_currency", "SAR")
        tax_number = request.form.get("tax_number", "").strip()
        vat_rate = float(request.form.get("vat_rate", 15))
        address = request.form.get("address", "").strip()
        if not name:
            flash("اسم الشركة مطلوب", "error")
            return render_template("companies/form.html")
        if any(c.name == name for c in current_user.companies):
            flash("يوجد شركة بنفس الاسم", "error")
            return render_template("companies/form.html")

        company = Company(
            name=name,
            base_currency=base_currency,
            tax_number=tax_number,
            vat_rate=vat_rate,
            address=address,
        )
        # Bug fix (abdelhamid) — companies created from the /companies/new
        # form also need the +1 month default subscription window.
        from app.services.subscription import activate_default_subscription
        activate_default_subscription(company)
        db.session.add(company)
        db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=current_user.id, company_id=company.id, role="owner",
        ))
        owner_emp = Employee(
            company_id=company.id,
            name=current_user.full_name,
            email=current_user.email,
        )
        db.session.add(owner_emp)
        db.session.flush()
        current_user.employee_id = owner_emp.id
        db.session.commit()
        seed_default_coa(company.id)
        # MARSOUD-32 — system roles + backfill the owner's user_companies row
        try:
            from app.services.roles_seed import ensure_roles_ready_for_company
            ensure_roles_ready_for_company(company.id)
        except Exception:
            current_app.logger.exception("seed system roles failed")
        # HR-05 — every new company starts with the 4 default leave types
        try:
            from app.services.leave import seed_default_leave_types
            seed_default_leave_types(company.id)
        except Exception:
            current_app.logger.exception("seed_default_leave_types failed")
        session["active_company_id"] = company.id
        flash("تم إنشاء الشركة وشجرة الحسابات الافتراضية", "success")
        return redirect(url_for("dashboard.index"))
    return render_template("companies/form.html")


@bp.route("/<int:company_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("company.edit")
def edit(company_id):
    company = db.session.get(Company, company_id)
    if not company or company not in current_user.companies:
        flash("غير مسموح", "error")
        return redirect(url_for("companies.index"))
    if request.method == "POST":
        # Logo upload (MARSOUD-23) — optional
        if "logo_file" in request.files:
            new_path = _save_logo(company, request.files["logo_file"])
            if new_path:
                company.logo_path = new_path

        # Remove existing logo if requested
        if request.form.get("remove_logo") == "1":
            if company.logo_path:
                # Best-effort delete on disk (don't fail save if missing)
                disk = os.path.join(current_app.root_path, company.logo_path.lstrip("/"))
                if os.path.exists(disk):
                    try:
                        os.remove(disk)
                    except OSError:
                        pass
            company.logo_path = None

        company.name = request.form.get("name", company.name).strip()
        company.tax_number = request.form.get("tax_number", company.tax_number)
        company.vat_rate = float(request.form.get("vat_rate", company.vat_rate))
        company.address = request.form.get("address", company.address)
        # MARSOUD-51 — bank info for invoice PDF (all optional)
        company.bank_name = (request.form.get("bank_name") or "").strip() or None
        company.bank_account_holder = (request.form.get("bank_account_holder") or "").strip() or None
        company.bank_account_number = (request.form.get("bank_account_number") or "").strip() or None
        company.iban = (request.form.get("iban") or "").strip() or None

        # ERP-03 — inventory + POS toggles.
        company.stock_strict_mode = request.form.get("stock_strict_mode") == "on"
        company.shift_required_for_pos = (
            request.form.get("shift_required_for_pos") == "on"
        )
        cm = (request.form.get("cost_method") or "AVERAGE").upper()
        if cm in ("AVERAGE", "FIFO"):
            company.cost_method = cm

        # Weekend config — checkbox group from the form
        if request.form.get("weekend_config_present") == "1":
            picked = request.form.getlist("weekend_day")
            cleaned = []
            for s in picked:
                try:
                    n = int(s)
                    if 0 <= n <= 6:
                        cleaned.append(str(n))
                except (TypeError, ValueError):
                    continue
            company.weekend_days = ",".join(sorted(set(cleaned), key=int)) or None

        # Reminder config (T13) — parse comma-separated day lists.
        def _parse_days(s):
            out = []
            for piece in (s or "").split(","):
                piece = piece.strip()
                if not piece:
                    continue
                try:
                    n = int(piece)
                    if n >= 0:
                        out.append(n)
                except ValueError:
                    pass
            return sorted(set(out), reverse=True)
        company.set_reminders({
            "enabled": request.form.get("reminders_enabled") == "1",
            "days_before": _parse_days(request.form.get("reminders_days_before", "7,3")),
            "overdue_days": _parse_days(request.form.get("reminders_overdue_days", "0")),
        })

        db.session.commit()
        flash("تم الحفظ", "success")
        return redirect(url_for("companies.index"))
    return render_template("companies/form.html", company=company)


# ─── MARSOUD-J: owner soft-deletes their own company ────────────────────
@bp.route("/<int:company_id>/soft-delete", methods=["POST"])
@login_required
def soft_delete(company_id):
    """Only the company's owner (per the membership row) can fire this.
    Captures the reason text + redirects to a different company in the
    user's switcher (or /companies/new if it was their only one)."""
    from app.models.user import user_companies
    from app.services.lifecycle import soft_delete_company
    company = db.session.get(Company, company_id)
    if not company:
        flash("الشركة غير موجودة", "error")
        return redirect(url_for("companies.index"))
    # Ownership check — must be an owner row in user_companies.
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == current_user.id) &
            (user_companies.c.company_id == company.id) &
            (user_companies.c.role == "owner")
        )
    ).first()
    if not row:
        flash("فقط مالك الشركة يقدر يحذفها", "error")
        return redirect(url_for("companies.index"))
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("لازم تكتب سبب الحذف", "error")
        return redirect(url_for("companies.edit", company_id=company.id))
    soft_delete_company(company, actor_id=current_user.id, reason=reason)
    # Drop the now-deleted company from session selection.
    from flask import session as _s
    _s.pop("active_company_id", None)
    flash(f"تم حذف الشركة '{company.name}'. للاستعادة تواصل مع الإدارة.",
          "success")
    # Pick another active company (if any) or send to the create page.
    other = [c for c in current_user.companies
             if c.id != company.id and c.deleted_at is None
             and (c.status or "ACTIVE") != "SUSPENDED"]
    if other:
        _s["active_company_id"] = other[0].id
        return redirect(url_for("dashboard.index"))
    return redirect(url_for("companies.new"))
