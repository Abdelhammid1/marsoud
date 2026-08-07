"""Fixed asset operations: purchase posting + monthly depreciation
+ disposal.

Depreciation is tracked per-asset, per-period via the DepreciationEntry table.
Posting the same month twice is impossible — the second attempt either skips
the already-processed assets (if mixed with new ones) or returns a clear
message saying nothing's left to do.

Disposal (MARSOUD-ASSET-DISPOSAL-01, 2026-08-07) closes an asset with one
balanced journal that reverses cost + accumulated depreciation + records
proceeds and the loss/gain differential. Once disposed,
`post_monthly_depreciation` naturally skips it (its filter is
`is_disposed=False` already — no touch needed there).
"""
from datetime import date, datetime
from app import db
from app.models import (
    FixedAsset, DepreciationEntry, DisposalReason,
)
from app.services.ledger import post_journal, get_account_by_code, LedgerError


class AssetError(Exception):
    """User-facing validation error on the assets flow. Routes catch
    this alongside LedgerError and flash + redirect back."""


# MARSOUD-COA-REBUILD — 1120 (banks) and 2110 (AP) are now headers and
# refuse posting. We route to leaves the new tree guarantees exist:
#   bank   → first available bank account (1124 default)
#   credit → 2115 Notes Payable (a leaf — vendor sub-accounts are only
#            wired into the AP flow because vendor identity is required
#            and FixedAsset has no vendor concept)
FUNDING_ACCOUNT_CODES = {
    "cash": "1110",
    "bank_fallback_chain": ("1124", "1121", "1122", "1123", "1125"),
    "credit": "2115",
}


def post_asset_purchase(asset, funding="cash", created_by=None):
    """Dr Fixed Asset / Cr Cash (or Bank, or Notes Payable)."""
    if float(asset.cost or 0) <= 0:
        return None
    if funding == "bank":
        source = None
        for code in FUNDING_ACCOUNT_CODES["bank_fallback_chain"]:
            source = get_account_by_code(asset.company_id, code)
            if source:
                break
        if not source:
            raise LedgerError(
                "لا يوجد حساب بنك مفعّل — أضف بنكاً تحت 1120 أولاً"
            )
    else:
        source_code = FUNDING_ACCOUNT_CODES.get(funding, "1110")
        source = get_account_by_code(asset.company_id, source_code)
        if not source:
            raise LedgerError(f"حساب التمويل ({source_code}) غير موجود")

    return post_journal(
        company_id=asset.company_id,
        description=f"شراء أصل ثابت: {asset.name}",
        lines=[
            {"account_id": asset.account_id, "debit": float(asset.cost), "credit": 0, "memo": "تكلفة الأصل"},
            {"account_id": source.id, "debit": 0, "credit": float(asset.cost), "memo": "تمويل الشراء"},
        ],
        entry_date=asset.purchase_date,
        reference=f"ASSET-{asset.id}",
        created_by=created_by,
        source_type="asset_purchase",
        source_id=asset.id,
    )


def post_monthly_depreciation(company_id, year, month, created_by=None):
    """Post one journal per asset that hasn't been depreciated for this period.

    Returns a dict:
        {
          "processed": [(asset_name, amount), ...],
          "skipped":   [asset_name, ...],   # already done for this period
          "total_amount": float,
        }

    Idempotent — calling twice in the same month for the same assets is a no-op.
    """
    assets = FixedAsset.query.filter_by(company_id=company_id, is_disposed=False).all()

    processed = []
    skipped = []
    total_amount = 0.0

    if not assets:
        return {"processed": [], "skipped": [], "total_amount": 0.0}

    dep_expense = get_account_by_code(company_id, "5250")
    accumulated = get_account_by_code(company_id, "1290")
    if not dep_expense or not accumulated:
        raise LedgerError("حسابات الإهلاك غير موجودة في شجرة الحسابات")

    for asset in assets:
        if asset.depreciated_for_period(year, month):
            skipped.append(asset.name)
            continue

        monthly = asset.monthly_depreciation
        if monthly <= 0:
            skipped.append(asset.name)
            continue

        # Cap: don't depreciate past the recoverable amount
        max_more = float(asset.cost) - float(asset.salvage_value) - float(asset.accumulated_depreciation or 0)
        if max_more <= 0.01:
            skipped.append(asset.name)
            continue
        amount = min(monthly, max_more)

        # Post the journal
        entry = post_journal(
            company_id=company_id,
            description=f"إهلاك أصل ثابت: {asset.name} — {month:02d}/{year}",
            lines=[
                {"account_id": dep_expense.id, "debit": amount, "credit": 0,
                 "memo": f"مصاريف إهلاك الأصل {asset.name}"},
                {"account_id": accumulated.id, "debit": 0, "credit": amount,
                 "memo": f"مجمع إهلاك الأصل {asset.name}"},
            ],
            reference=f"DEPR-{year}-{month:02d}-A{asset.id}",
            created_by=created_by,
            source_type="depreciation",
            source_id=asset.id,
        )

        # Update asset and create the period record
        asset.accumulated_depreciation = float(asset.accumulated_depreciation or 0) + amount
        new_nbv = float(asset.cost) - float(asset.accumulated_depreciation)
        db.session.add(DepreciationEntry(
            asset_id=asset.id,
            period_year=year, period_month=month,
            amount=amount,
            journal_entry_id=entry.id,
            book_value_after=new_nbv,
        ))
        processed.append((asset.name, amount))
        total_amount += amount

    db.session.commit()
    return {"processed": processed, "skipped": skipped, "total_amount": total_amount}


# ─── Disposal ───────────────────────────────────────────────────────────
# MARSOUD-ASSET-DISPOSAL-01 (2026-08-07). One function, one journal, one
# state flip. Everything else in the module already respects
# `is_disposed=True` (line 74 above), so setting the flag is sufficient
# to stop future depreciation with no other code paths to touch.
#
# The journal shape covers three cases with one algebra:
#
#   Dr 1290 accumulated_depreciation
#   Dr cash/bank                                (if proceeds > 0)
#   Dr 5950 loss OR charged_account_id          (if NBV > proceeds)
#   Cr 4550 gain                                (if proceeds > NBV)
#   Cr asset.account_id (cost)
#
# Algebra: Dr acc_dep + Dr proceeds + (Dr diff OR -Cr |diff|)
#          = Cr cost, always. See docstring on dispose_asset.
def dispose_asset(asset_id, *, disposal_date, reason,
                  proceeds=0, charged_account_id=None,
                  note=None, funding="cash", created_by=None):
    """Close a FixedAsset with the disposal journal + flag flip.

    Args:
      asset_id: FixedAsset.id (route-side already cross-tenant checked)
      disposal_date: date the asset left the books
      reason: DisposalReason enum value (SOLD / LOST / DAMAGED /
              END_OF_LIFE / OTHER) OR the raw string
      proceeds: cash/bank received on sale (0 for a loss/scrap)
      charged_account_id: OPTIONAL — if passed, the loss (diff > 0)
          hits this account instead of 5950. The seam
          MARSOUD-ITEM-CUSTODY-01 uses to charge a lost asset's
          NBV to the employee's 2130 sub-account. The assets
          module NEVER learns about custody — the caller resolves
          the account.
      note: free-text detail (mandatory-ish when reason=OTHER but
            we don't enforce; UI nudges the operator)
      funding: "cash" / "bank" — where proceeds landed (only used
          when proceeds > 0). Same vocabulary as post_asset_purchase.
      created_by: user id for audit + reversal_entry_id trail

    Returns the same FixedAsset (with `.is_disposed=True` and all
    5 disposal_* columns populated + commit).

    Raises AssetError on double-dispose or missing accounts. Raises
    LedgerError from post_journal on any balance drift (should be
    impossible by construction — see algebra above)."""
    # 1. Load + validate
    asset = db.session.get(FixedAsset, int(asset_id))
    if not asset:
        raise AssetError("الأصل غير موجود")
    if asset.is_disposed:
        # Give a helpful message — the operator needs to know WHEN
        # the earlier disposal happened, not just "already done".
        prev = (asset.disposal_date.isoformat()
                if asset.disposal_date else "بدون تاريخ مُسجَّل")
        raise AssetError(
            f"الأصل «{asset.name}» مشطوب بالفعل بتاريخ {prev} — "
            "لا يمكن شطبه مرتين. لو الشطب السابق غلط، اعمل قيد "
            "تصحيح يدوي.")

    # Normalise reason to the enum.
    if isinstance(reason, str):
        try:
            reason = DisposalReason(reason.upper())
        except ValueError:
            raise AssetError(f"سبب الشطب غير صالح: {reason!r}")
    if not isinstance(reason, DisposalReason):
        raise AssetError("سبب الشطب مطلوب")

    try:
        proceeds = round(float(proceeds or 0), 2)
    except (TypeError, ValueError):
        raise AssetError("مبلغ التحصيل غير صالح")
    if proceeds < 0:
        raise AssetError("مبلغ التحصيل لا يمكن أن يكون سالباً")

    cid = asset.company_id
    disposal_date = disposal_date or date.today()

    # 2. Resolve accounts.
    cost = float(asset.cost or 0)
    acc_dep = float(asset.accumulated_depreciation or 0)
    nbv = cost - acc_dep
    diff = round(nbv - proceeds, 2)   # >0 = loss, <0 = gain, 0 = neutral

    if not asset.account_id:
        raise AssetError("الأصل غير مرتبط بحساب — راجع سجل الأصل")

    accumulated = get_account_by_code(cid, "1290")
    if not accumulated:
        raise AssetError(
            "حساب مجمع الإهلاك (1290) غير موجود — راجع شجرة الحسابات")

    proceeds_account = None
    if proceeds > 0.005:
        if funding == "bank":
            for code in FUNDING_ACCOUNT_CODES["bank_fallback_chain"]:
                proceeds_account = get_account_by_code(cid, code)
                if proceeds_account:
                    break
            if not proceeds_account:
                raise AssetError(
                    "لا يوجد حساب بنك مفعّل — أضف بنكاً تحت 1120 أولاً")
        else:
            proceeds_account = get_account_by_code(cid, "1110")
            if not proceeds_account:
                raise AssetError(
                    "حساب النقدية (1110) غير موجود — راجع شجرة الحسابات")

    diff_account = None
    if diff > 0.005:
        # Loss. charged_account_id (item-custody's seam) wins over
        # the default 5950 when set.
        if charged_account_id:
            from app.models import Account
            ca = db.session.get(Account, int(charged_account_id))
            if not ca or ca.company_id != cid:
                raise AssetError("حساب تحميل الفرق غير موجود")
            if not ca.is_postable:
                raise AssetError(
                    "حساب تحميل الفرق غير قابل للترحيل (header) — "
                    "اختر حساب فرعي")
            diff_account = ca
        else:
            diff_account = get_account_by_code(cid, "5950")
            if not diff_account:
                raise AssetError(
                    "حساب خسائر استبعاد الأصول (5950) غير موجود — "
                    "شغّل seed_coa لإضافته")
    elif diff < -0.005:
        # Gain. 4550 always — a gain never belongs on a random
        # charged_account_id (item-custody uses the loss side only).
        diff_account = get_account_by_code(cid, "4550")
        if not diff_account:
            raise AssetError(
                "حساب أرباح استبعاد الأصول (4550) غير موجود — "
                "شغّل seed_coa لإضافته")

    # 3. Build lines. Skip zero-value legs so post_journal doesn't
    #    reject empty debit=0/credit=0 rows.
    lines = []
    if acc_dep > 0.005:
        lines.append({
            "account_id": accumulated.id,
            "debit": acc_dep, "credit": 0,
            "memo": f"عكس مجمع إهلاك الأصل «{asset.name}» عند الشطب",
        })
    if proceeds > 0.005:
        lines.append({
            "account_id": proceeds_account.id,
            "debit": proceeds, "credit": 0,
            "memo": f"تحصيل بيع أصل «{asset.name}»",
        })
    if diff > 0.005:
        lines.append({
            "account_id": diff_account.id,
            "debit": diff, "credit": 0,
            "memo": (f"خسارة استبعاد أصل «{asset.name}»"
                     if not charged_account_id
                     else f"تحميل قيمة أصل «{asset.name}» على حساب مُمرَّر"),
        })
    elif diff < -0.005:
        lines.append({
            "account_id": diff_account.id,
            "debit": 0, "credit": -diff,
            "memo": f"مكسب استبعاد أصل «{asset.name}»",
        })
    # Always: Cr the asset's cost account for the full cost.
    lines.append({
        "account_id": asset.account_id,
        "debit": 0, "credit": cost,
        "memo": f"شطب أصل ثابت «{asset.name}» بتكلفة {cost:.2f}",
    })

    # 4. Post. Balance is algebraic (acc_dep + proceeds + diff = cost).
    #    post_journal still verifies numerically — belt & braces.
    entry = post_journal(
        company_id=cid,
        description=(f"شطب أصل ثابت: {asset.name} — "
                     f"{reason.label_ar} — NBV={nbv:.2f}, "
                     f"proceeds={proceeds:.2f}"),
        lines=lines,
        entry_date=disposal_date,
        reference=f"DISPOSE-A{asset.id}",
        created_by=created_by,
        source_type="asset_disposal",
        source_id=asset.id,
    )

    # 5. Flip the flag + record the closure metadata.
    asset.is_disposed = True
    asset.disposal_date = disposal_date
    asset.disposal_reason = reason
    asset.disposal_note = (note or "").strip() or None
    asset.disposal_proceeds = proceeds
    asset.disposal_journal_entry_id = entry.id
    asset.disposed_by_id = created_by
    db.session.commit()
    return asset
