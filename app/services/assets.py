"""Fixed asset operations: purchase posting + monthly depreciation.

Depreciation is tracked per-asset, per-period via the DepreciationEntry table.
Posting the same month twice is impossible — the second attempt either skips
the already-processed assets (if mixed with new ones) or returns a clear
message saying nothing's left to do.
"""
from datetime import date
from app import db
from app.models import FixedAsset, DepreciationEntry
from app.services.ledger import post_journal, get_account_by_code, LedgerError


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
