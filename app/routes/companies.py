import os
from werkzeug.utils import secure_filename
from flask import Blueprint, render_template, redirect, url_for, flash, request, session, g, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Company
from app.models.user import user_companies
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
    return render_template("companies/index.html", companies=current_user.companies)


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
        db.session.add(company)
        db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=current_user.id, company_id=company.id, role="owner",
        ))
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
