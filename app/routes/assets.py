from datetime import datetime, date
from flask import Blueprint, render_template, redirect, url_for, flash, request, g
from flask_login import login_required, current_user
from app import db
from app.models import (
    FixedAsset, DepreciationEntry, Account, Vendor, DisposalReason,
)
from app.services.assets import (
    post_monthly_depreciation, post_asset_purchase,
    dispose_asset, AssetError,
)
from app.services.ledger import LedgerError
from app.services.permissions import require_permission

bp = Blueprint("assets", __name__)


@bp.route("/")
@login_required
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))

    today = date.today()
    q = FixedAsset.query.filter_by(company_id=g.active_company.id)

    # Filters
    status_filter = request.args.get("status")
    type_filter = request.args.get("type")
    if type_filter:
        q = q.filter(FixedAsset.account_id == int(type_filter))

    assets = q.order_by(FixedAsset.created_at.desc()).all()

    # Compute "depreciated this month" status per asset (in Python — cheap for typical sizes)
    period_status = {
        a.id: a.depreciated_for_period(today.year, today.month) for a in assets
    }
    # Status filter applied after computing
    if status_filter == "done":
        assets = [a for a in assets if period_status[a.id]]
    elif status_filter == "pending":
        assets = [a for a in assets if not period_status[a.id]]

    asset_accounts = Account.query.filter(
        Account.company_id == g.active_company.id,
        Account.code.like("12%"),
    ).order_by(Account.code).all()

    return render_template(
        "assets/index.html",
        assets=assets, period_status=period_status,
        asset_accounts=asset_accounts,
        current_year=today.year, current_month=today.month,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("assets.manage")
def new():
    fixed_accounts = Account.query.filter(
        Account.company_id == g.active_company.id,
        Account.code.like("12%"),
        Account.code != "1290",   # don't allow posting cost to accumulated depreciation
    ).all()
    vendors = Vendor.query.filter_by(company_id=g.active_company.id, is_active=True).order_by(Vendor.name).all()
    if request.method == "POST":
        try:
            name = request.form.get("name", "").strip()
            purchase_date = datetime.strptime(request.form.get("purchase_date", date.today().isoformat()), "%Y-%m-%d").date()
            cost = float(request.form.get("cost", 0))
            salvage = float(request.form.get("salvage_value", 0))
            life = int(request.form.get("useful_life_years", 1))
            account_id = int(request.form.get("account_id"))
            funding = request.form.get("funding", "cash")
            vendor_id = request.form.get("vendor_id") or None
            if life <= 0:
                raise ValueError("العمر الإنتاجي يجب أن يكون أكبر من صفر")
            asset = FixedAsset(
                company_id=g.active_company.id,
                name=name,
                purchase_date=purchase_date,
                cost=cost,
                salvage_value=salvage,
                useful_life_years=life,
                account_id=account_id,
                vendor_id=int(vendor_id) if vendor_id else None,
            )
            db.session.add(asset)
            db.session.flush()
            post_asset_purchase(asset, funding=funding, created_by=current_user.id)
            db.session.commit()
            flash("تم تسجيل الأصل وقيد الشراء", "success")
            return redirect(url_for("assets.view", asset_id=asset.id))
        except (ValueError, KeyError, LedgerError) as e:
            db.session.rollback()
            flash(str(e), "error")
    return render_template("assets/form.html", fixed_accounts=fixed_accounts, vendors=vendors)


@bp.route("/<int:asset_id>")
@login_required
def view(asset_id):
    asset = db.session.get(FixedAsset, asset_id)
    if not asset or asset.company_id != g.active_company.id:
        return redirect(url_for("assets.index"))
    history = DepreciationEntry.query.filter_by(asset_id=asset.id).order_by(
        DepreciationEntry.period_year, DepreciationEntry.period_month
    ).all()
    return render_template(
        "assets/view.html", asset=asset, history=history,
        disposal_reasons=DisposalReason)


# MARSOUD-ASSET-DISPOSAL-01 (2026-08-07) — the irreversible closure.
# Cross-tenant guard by loading with company scope BEFORE calling
# the service (service also checks but this keeps the URL 404
# rather than 500-with-service-error for company B poking A's id).
@bp.route("/<int:asset_id>/dispose", methods=["POST"])
@login_required
@require_permission("assets.dispose")
def dispose(asset_id):
    asset = db.session.get(FixedAsset, asset_id)
    if not asset or asset.company_id != g.active_company.id:
        flash("الأصل غير موجود", "error")
        return redirect(url_for("assets.index"))
    try:
        _date_raw = request.form.get("disposal_date", "").strip()
        _date = (datetime.strptime(_date_raw, "%Y-%m-%d").date()
                 if _date_raw else date.today())
        dispose_asset(
            asset.id,
            disposal_date=_date,
            reason=request.form.get("reason") or "OTHER",
            proceeds=request.form.get("proceeds") or 0,
            funding=request.form.get("funding", "cash"),
            note=request.form.get("note"),
            # charged_account_id is a service seam that no UI field
            # populates in this ticket (item-custody will pass it).
            # Left None here on purpose.
            created_by=current_user.id,
        )
        flash(
            f"تم شطب الأصل «{asset.name}» — القيد رقم "
            f"{asset.disposal_journal_entry_id}",
            "success")
    except (AssetError, LedgerError, ValueError, TypeError) as e:
        db.session.rollback()
        flash(str(e), "error")
    return redirect(url_for("assets.view", asset_id=asset.id))


@bp.route("/depreciate", methods=["POST"])
@login_required
@require_permission("assets.manage")
def depreciate():
    today = date.today()
    year = int(request.form.get("year", today.year))
    month = int(request.form.get("month", today.month))
    try:
        result = post_monthly_depreciation(
            g.active_company.id, year, month, created_by=current_user.id
        )
        processed = result["processed"]
        skipped = result["skipped"]

        if processed and skipped:
            names_done = "، ".join(n for n, _ in processed)
            names_skip = "، ".join(skipped)
            flash(
                f"تم تسجيل إهلاك الأصول: {names_done} — "
                f"الأصول التالية سبق تسجيل إهلاكها هذا الشهر: {names_skip}",
                "success"
            )
        elif processed:
            names_done = "، ".join(n for n, _ in processed)
            flash(f"تم تسجيل إهلاك {len(processed)} أصل: {names_done} (إجمالي {result['total_amount']:.2f})", "success")
        elif skipped:
            flash(f"كل الأصول لها إهلاك مسجل لشهر {month}/{year} بالفعل — لا يوجد ما يجب تسجيله", "info")
        else:
            flash("لا توجد أصول قابلة للإهلاك", "info")
    except LedgerError as e:
        flash(str(e), "error")
    return redirect(url_for("assets.index"))
