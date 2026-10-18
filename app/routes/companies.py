import os
import re
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
        # MARSOUD-CURRENCY-TAX-DEFAULTS (Batch 8 Ticket 4b,
        # 2026-07-30) — new sub-companies default to 0% VAT
        # (same policy as /register). Owner can override in the
        # form. Existing companies keep their saved rate.
        vat_rate = float(request.form.get("vat_rate", 0))
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
        # MARSOUD-MC-EMPLOYEE — link the owner's Employee row directly
        # to their User via Employee.user_id. The old code overwrote the
        # single users.employee_id scalar every time a new company was
        # created, breaking /my/ access in all previously-created
        # companies (last-write-wins). Now the FK lives on Employee
        # where it's naturally per-company.
        owner_emp = Employee(
            company_id=company.id,
            name=current_user.full_name,
            email=current_user.email,
            user_id=current_user.id,
        )
        db.session.add(owner_emp)
        db.session.commit()
        seed_default_coa(company.id)
        # MARSOUD-32 — system roles + backfill the owner's user_companies row
        try:
            from app.services.roles_seed import ensure_roles_ready_for_company
            ensure_roles_ready_for_company(company.id)
            # MARSOUD-ROLE-SYNC — the insert above wrote only the string.
            # Now that the Role rows exist, write both columns explicitly
            # instead of leaning on the NULL-only boot backfill.
            from app.services.roles import set_membership_role
            set_membership_role(current_user.id, company.id, "owner")
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
        # MARSOUD-COMPANY-LEGAL — three optional legal fields. Any
        # empty submission becomes NULL so the display fallback chain
        # (brand → legal → name) kicks in cleanly.
        company.legal_name = (
            request.form.get("legal_name") or "").strip() or None
        company.brand_name = (
            request.form.get("brand_name") or "").strip() or None
        company.commercial_register_no = (
            request.form.get("commercial_register_no") or "").strip() or None
        # MARSOUD-51 — bank info for invoice PDF (all optional)
        # MARSOUD-INVOICE-PDF-POLISH-01 (2026-09-10) — the form used
        # to render `value="{{ company.iban if company else '' }}"`
        # which spat out the literal string "None" when a field was
        # empty; a subsequent save re-read "None" as data and stored
        # it, poisoning the PDF's truthy guards. `_clean_channel`
        # normalises both an empty submit AND the "None" ghost back
        # to real Python None so historically-dirtied rows self-heal
        # the next time the owner saves.
        def _clean_channel(name):
            v = (request.form.get(name) or "").strip()
            if v.lower() == "none":
                v = ""
            return v or None
        company.bank_name = _clean_channel("bank_name")
        company.bank_account_holder = _clean_channel("bank_account_holder")
        company.bank_account_number = _clean_channel("bank_account_number")
        company.iban = _clean_channel("iban")
        # MARSOUD-INVOICE-PAYMENT-CHANNELS-01 — InstaPay + e-wallet.
        # Same "" → None normalisation so an empty submit clears the
        # field cleanly and doesn't leave a whitespace ghost.
        company.instapay_handle = _clean_channel("instapay_handle")
        company.ewallet_number = _clean_channel("ewallet_number")
        company.ewallet_provider = _clean_channel("ewallet_provider")

        # MARSOUD-INVOICE-BRAND-COLOR (2026-09-26) — tenant-chosen PDF
        # brand color. Accept only strict `#RRGGBB` hex (case-insensitive)
        # so the value can be safely inlined into the PDF template's
        # CSS/HTML without an escape pass. An empty submit clears the
        # setting, which restores the app default (#059669) on the next
        # render — matches how every other "optional PDF appearance"
        # setting on this page behaves.
        _raw_color = (request.form.get("invoice_brand_color") or "").strip()
        if _raw_color and re.fullmatch(r"#[0-9A-Fa-f]{6}", _raw_color):
            company.invoice_brand_color = _raw_color.upper()
        elif not _raw_color:
            company.invoice_brand_color = None
        # else: ignore junk input silently, keep whatever was saved.

        # MARSOUD-TZ-01 — company-level timezone. Falls back to
        # existing value (default "Asia/Riyadh") if the field is
        # missing or blank.
        tz_new = (request.form.get("timezone") or "").strip()
        if tz_new:
            company.timezone = tz_new

        # ERP-03 — inventory + POS toggles.
        company.stock_strict_mode = request.form.get("stock_strict_mode") == "on"
        company.shift_required_for_pos = (
            request.form.get("shift_required_for_pos") == "on"
        )
        cm = (request.form.get("cost_method") or "AVERAGE").upper()
        if cm in ("AVERAGE", "FIFO"):
            company.cost_method = cm

        # MARSOUD-LOYALTY-POINTS-01 — per-tenant loyalty settings.
        # Rates are only clamped, not rejected — an owner who wants a
        # very stingy or very generous program can pick anything > 0.
        company.loyalty_enabled = (
            request.form.get("loyalty_enabled") == "on")
        try:
            er = float(request.form.get("loyalty_earn_rate") or 0)
            if er > 0:
                company.loyalty_earn_rate = er
        except (TypeError, ValueError):
            pass
        try:
            rv = float(request.form.get("loyalty_redemption_value") or 0)
            if rv > 0:
                company.loyalty_redemption_value = rv
        except (TypeError, ValueError):
            pass

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


# ─── MARSOUD-COMPANY-BRANCHES-01 (2026-09-02) — link a company as
# a branch of another same-owner company, list current branches,
# or unlink. Reuses Company.parent_id + count_branches +
# QUOTA_BRANCHES that already exist in the DB.
@bp.route("/<int:company_id>/branches", methods=["GET", "POST"])
@login_required
@require_permission("company.edit")
def branches(company_id):
    company = db.session.get(Company, company_id)
    if not company or company not in current_user.companies:
        flash("الشركة غير موجودة", "error")
        return redirect(url_for("companies.index"))

    # BRANCH MODE — this company already has a parent. Show it +
    # allow the owner to unlink it. Historical data stays intact.
    if company.parent_id:
        parent = db.session.get(Company, company.parent_id)
        if request.method == "POST" \
                and request.form.get("action") == "unlink":
            company.parent_id = None
            db.session.commit()
            flash("تم فك ربط الفرع", "success")
            return redirect(url_for("companies.branches",
                                     company_id=company.id))
        return render_template("companies/branches.html",
                                company=company, parent=parent,
                                sub_branches=None, eligible=[])

    # PARENT MODE — either linking a new branch or viewing existing.
    sub_branches = (Company.query
                     .filter_by(parent_id=company.id)
                     .order_by(Company.name).all())
    if request.method == "POST" \
            and request.form.get("action") == "link":
        branch_id = request.form.get("branch_company_id", type=int)
        branch = db.session.get(Company, branch_id) if branch_id else None

        # Cross-tenant guard — ticket §9. Without this, knowing an
        # id lets a user pull another owner's data into their
        # consolidated report.
        if not branch or branch not in current_user.companies:
            flash("الشركة المختارة غير صالحة", "error")
            return redirect(url_for("companies.branches",
                                     company_id=company.id))
        if branch.id == company.id:
            flash("لا يمكن ربط الشركة بنفسها", "error")
            return redirect(url_for("companies.branches",
                                     company_id=company.id))
        # Hierarchy guard — refuse a company that's already linked
        # elsewhere (multi-level chains disallowed in v1, ticket §4).
        if branch.parent_id is not None:
            flash(f"'{branch.name}' مرتبطة بالفعل كفرع لشركة أخرى",
                  "error")
            return redirect(url_for("companies.branches",
                                     company_id=company.id))
        # Also refuse a company that already has children of its own
        # — otherwise the resulting shape is multi-level.
        if Company.query.filter_by(parent_id=branch.id).first():
            flash(
                f"'{branch.name}' هي نفسها شركة أم لفروع — "
                "لا يمكن ربطها كفرع لشركة أخرى",
                "error")
            return redirect(url_for("companies.branches",
                                     company_id=company.id))

        # Plan quota — ticket §9. QUOTA_BRANCHES is already
        # enforced in check_quota; QuotaBlockedError bubbles as a
        # flash message.
        try:
            from app.services.quotas import (
                check_quota, QuotaBlockedError,
            )
            from app.models import QUOTA_BRANCHES
            check_quota(company, QUOTA_BRANCHES, incoming=1)
        except QuotaBlockedError as e:
            flash(str(e), "error")
            return redirect(url_for("companies.branches",
                                     company_id=company.id))

        branch.parent_id = company.id
        db.session.commit()
        flash(f"تم إضافة '{branch.name}' كفرع", "success")
        return redirect(url_for("companies.branches",
                                 company_id=company.id))

    # GET — build the eligible-branch dropdown: owned by the user,
    # not self, not already a branch, not itself a parent.
    child_parent_ids = {c.parent_id for c in sub_branches}
    eligible = []
    for c in current_user.companies:
        if c.id == company.id:
            continue
        if c.parent_id is not None:
            continue
        if Company.query.filter_by(parent_id=c.id).first():
            continue
        eligible.append(c)
    return render_template(
        "companies/branches.html",
        company=company, parent=None,
        sub_branches=sub_branches, eligible=eligible)


# ─── MARSOUD-SMTP-PER-COMPANY-01 (2026-10-18) ──────────────────────────
# Per-company SMTP settings UI + test-connection endpoint.  Reuses the
# `company.edit` permission (sensitive data belongs to the same
# tenant-config scope as the main edit form, and we don't want to
# invent a new permission just for one sub-page).  Cross-tenant guard
# matches the pattern in `edit()` above: `company not in current_user.companies`.
_SMTP_ENC_FORM_VALUES = ("STARTTLS", "SSL", "NONE")


def _smtp_assert_access(company_id):
    """Shared cross-tenant guard for the three SMTP routes.  Returns
    the company on success, or (None, redirect) on refusal."""
    company = db.session.get(Company, company_id)
    if not company or company not in current_user.companies:
        flash("غير مسموح", "error")
        return None, redirect(url_for("companies.index"))
    return company, None


@bp.route("/<int:company_id>/smtp-settings",
          methods=["GET", "POST"])
@login_required
@require_permission("company.edit")
def smtp_settings(company_id):
    """Render the SMTP settings form (GET) or save it (POST).

    The password field is NEVER populated from the stored value — on
    GET it renders blank, and on POST we only overwrite the stored
    password when the operator typed a new one.  Partial edits (host
    change, keep password) do not nuke the saved credential.
    """
    from app.models import CompanySMTP
    from app.services.crypto import encrypt
    company, bail = _smtp_assert_access(company_id)
    if bail is not None:
        return bail
    smtp = (CompanySMTP.query
            .filter_by(company_id=company.id).first())

    if request.method == "POST":
        host = (request.form.get("host") or "").strip()
        port_raw = (request.form.get("port") or "").strip()
        encryption = (request.form.get("encryption") or "STARTTLS").upper()
        username = (request.form.get("username") or "").strip() or None
        new_password = request.form.get("password") or ""
        from_email = (request.form.get("from_email") or "").strip()
        from_name = (request.form.get("from_name") or "").strip() or None
        is_enabled = request.form.get("is_enabled") == "1"

        # Validate.  No encryption → log but keep plain SMTP so dev
        # mail-trap setups still work; UI warns.
        if not host:
            flash("خادم SMTP مطلوب", "error")
            return redirect(url_for("companies.smtp_settings",
                                      company_id=company.id))
        try:
            port = int(port_raw)
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            flash("رقم المنفذ غير صالح (1–65535)", "error")
            return redirect(url_for("companies.smtp_settings",
                                      company_id=company.id))
        if encryption not in _SMTP_ENC_FORM_VALUES:
            flash("نوع التشفير غير صالح", "error")
            return redirect(url_for("companies.smtp_settings",
                                      company_id=company.id))
        if not from_email or "@" not in from_email:
            flash("البريد الإلكتروني للمرسل غير صالح", "error")
            return redirect(url_for("companies.smtp_settings",
                                      company_id=company.id))

        if smtp is None:
            smtp = CompanySMTP(company_id=company.id,
                                host=host, port=port,
                                encryption=encryption,
                                username=username,
                                from_email=from_email,
                                from_name=from_name,
                                is_enabled=is_enabled)
            # New rows: a password MUST be provided (unless the
            # server is open-relay / no-auth — detectable only
            # at runtime, so warn, don't block).
            if new_password:
                smtp.password_encrypted = encrypt(new_password)
            db.session.add(smtp)
        else:
            smtp.host = host
            smtp.port = port
            smtp.encryption = encryption
            smtp.username = username
            smtp.from_email = from_email
            smtp.from_name = from_name
            smtp.is_enabled = is_enabled
            # Only touch the password blob when the operator typed
            # something new — empty submit keeps the stored secret.
            if new_password:
                smtp.password_encrypted = encrypt(new_password)
        db.session.commit()
        flash("تم حفظ إعدادات SMTP", "success")
        return redirect(url_for("companies.smtp_settings",
                                 company_id=company.id))

    return render_template("companies/smtp_settings.html",
                            company=company, smtp=smtp,
                            enc_choices=_SMTP_ENC_FORM_VALUES)


@bp.route("/<int:company_id>/smtp-settings/test",
          methods=["POST"])
@login_required
@require_permission("company.edit")
def smtp_test(company_id):
    """Test the SMTP config by connecting, authenticating, AND sending
    a tiny test email to the operator who clicked the button.

    Accepts form fields (host/port/encryption/username/password/
    from_email/from_name) — if ANY of those are submitted, the test
    uses THOSE values (pre-save test, per ticket AC #3 "يمكن اختبار
    إعدادات SMTP قبل اعتمادها").  If no form fields come in, the
    test uses the stored row.

    Only stamps `last_test_*` on the stored row — pre-save tests are
    not persisted (they're meant to validate before saving).
    """
    from app.models import CompanySMTP
    from app.services.crypto import decrypt
    from email.mime.text import MIMEText
    from email.utils import formataddr
    import smtplib
    from datetime import datetime as _dt
    company, bail = _smtp_assert_access(company_id)
    if bail is not None:
        return bail
    smtp = (CompanySMTP.query
            .filter_by(company_id=company.id).first())

    # Prefer form-submitted values when present (pre-save test).  A
    # missing `password` field falls back to the stored encrypted
    # blob — operator can tweak host/port without re-entering the
    # password.
    form_host = (request.form.get("host") or "").strip()
    using_form = bool(form_host)

    if using_form:
        host = form_host
        try:
            port = int(request.form.get("port") or 0)
        except ValueError:
            port = 0
        enc = (request.form.get("encryption") or "STARTTLS").upper()
        # Defense-in-depth: whitelist the encryption value so a crafted
        # POST can't downgrade the test (or the saved row's live sends)
        # to a mode we didn't branch on.
        if enc not in _SMTP_ENC_FORM_VALUES:
            flash("نوع التشفير غير صالح", "error")
            return redirect(url_for("companies.smtp_settings",
                                     company_id=company.id))
        user = (request.form.get("username") or "").strip()
        form_pw = request.form.get("password") or ""
        # Security guard — the pre-save test could become a password-
        # exfiltration tool if the operator swapped the host but left
        # the password blank: we would send the STORED password to the
        # attacker's SMTP server during AUTH LOGIN.  Only trust the
        # stored password when the submitted host + port + username
        # EXACTLY match the saved row.  Otherwise, demand that the
        # operator types the password into the form.
        if form_pw:
            pw = form_pw
        elif (smtp and smtp.password_encrypted
                and host == smtp.host
                and port == int(smtp.port)
                and user == (smtp.username or "")):
            pw = decrypt(smtp.password_encrypted) or ""
        elif smtp and smtp.password_encrypted:
            flash(
                "أدخل كلمة المرور عند اختبار خادم أو مستخدم مختلف "
                "عن المحفوظ — كلمة المرور المخزّنة لا تُرسل لخوادم "
                "أخرى.", "error")
            return redirect(url_for("companies.smtp_settings",
                                     company_id=company.id))
        else:
            pw = ""
        from_email = (request.form.get("from_email") or "").strip()
        from_name = ((request.form.get("from_name") or "").strip()
                      or from_email)
        if not host or not port or not from_email:
            flash("البيانات ناقصة — أدخل الخادم والمنفذ وبريد المرسل",
                  "error")
            return redirect(url_for("companies.smtp_settings",
                                     company_id=company.id))
    else:
        if smtp is None:
            flash("لا توجد إعدادات SMTP للاختبار", "warning")
            return redirect(url_for("companies.smtp_settings",
                                     company_id=company.id))
        host = smtp.host
        port = int(smtp.port)
        user = smtp.username or ""
        pw = decrypt(smtp.password_encrypted) or ""
        enc = (smtp.encryption or "STARTTLS").upper()
        from_email = smtp.from_email
        from_name = smtp.from_name or smtp.from_email

    # Target the operator's own address — guarantees the test lands
    # somewhere they can actually inspect.
    to_addr = (current_user.email or from_email)
    msg = MIMEText(
        f"<p>هذا بريد اختباري من إعدادات SMTP الخاصة بشركة "
        f"<b>{company.name}</b>.</p>"
        f"<p>إذا وصلك هذا البريد فإعدادات SMTP تعمل بشكل صحيح.</p>"
        f"<p><small>تم الإرسال في "
        f"{_dt.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</small></p>",
        "html", "utf-8")
    msg["Subject"] = f"اختبار SMTP — {company.name}"
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = to_addr

    ok, err_text = True, None
    try:
        if enc == "SSL":
            ctx = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            ctx = smtplib.SMTP(host, port, timeout=15)
        with ctx as server:
            if enc == "STARTTLS":
                server.starttls()
            if user:
                server.login(user, pw)
            # AC #3 fix: actually SEND a message, not just NOOP.
            # Relay-policy rejections (sender not permitted, etc.)
            # only surface on a real send.
            server.send_message(msg)
    except Exception as e:
        ok = False
        err_text = f"{type(e).__name__}: {str(e)[:300]}"

    # Stamp only when testing the saved row.  Pre-save tests show the
    # result via flash and don't persist.
    if not using_form and smtp is not None:
        smtp.last_test_at = _dt.utcnow()
        smtp.last_test_ok = ok
        smtp.last_test_error = err_text if not ok else None
        db.session.commit()

    if ok:
        flash(
            f"نجح الاختبار — تم إرسال بريد تجريبي إلى {to_addr}",
            "success")
    else:
        flash(f"فشل الاختبار: {err_text}", "error")
    return redirect(url_for("companies.smtp_settings",
                             company_id=company.id))


@bp.route("/<int:company_id>/smtp-settings/delete",
          methods=["POST"])
@login_required
@require_permission("company.edit")
def smtp_delete(company_id):
    """Remove the per-company SMTP row entirely so the next send
    falls back to the platform default — the ticket's "عند حذف
    إعدادات الشركة، يعود النظام تلقائيًا إلى SMTP مرصود" requirement."""
    from app.models import CompanySMTP
    company, bail = _smtp_assert_access(company_id)
    if bail is not None:
        return bail
    smtp = (CompanySMTP.query
            .filter_by(company_id=company.id).first())
    if smtp is None:
        flash("لا توجد إعدادات SMTP لحذفها", "info")
    else:
        db.session.delete(smtp)
        db.session.commit()
        flash("تم حذف إعدادات SMTP — ستُستخدم إعدادات مرصود الافتراضية",
              "success")
    return redirect(url_for("companies.smtp_settings",
                             company_id=company.id))


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
