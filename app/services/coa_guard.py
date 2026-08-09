"""MARSOUD-COA-REBUILD — chart-of-accounts safety net.

If any of these required accounts is missing from a company, the
service layer will throw at the first transaction with a confusing
error like "AttributeError: 'NoneType' object has no attribute 'id'".
verify_coa() lists exactly what's missing up-front, so the user can
re-seed before they lose time.
"""
from app.models import Account


# Every account the service-layer code looks up by literal code.
REQUIRED_ACCOUNTS = [
    # Cash + a default bank leaf (1124 is the default the seed picks).
    "1110", "1124",
    # Headers we depend on for sub-account placement.
    "1120", "1130", "2110", "2130",
    # Asset side
    "1280", "1290", "1300",
    # Equity / opening + retained
    "3900",
    # Revenue + contras
    "4100", "4300",
    # VAT split
    "2120", "2125",
    # Salary expense + commissions
    "5100", "5210", "5250", "5280", "5990",
    # Notes payable — used for asset purchases on credit
    "2115",
    # Sales commission liability
    "2150",
    # MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08, Phase 2) — every
    # account the 18 new hub processors + the payroll GOSI split
    # touch by literal code. If a tenant is missing any of these,
    # the corresponding wizard raises a friendly CoA error instead
    # of the raw NoneType.
    "1140", "1170",                        # notes receivable, accrued revenue
    "2135", "2136", "5217",                # GOSI + income-tax withholding
    "2140", "2210", "5940",                # short/long-term loans + interest
    "2170", "2190", "2220",                # deposits, dividends payable, EOSB provision
    "3300", "3400", "3500",                # retained earnings, current P&L, legal reserve
    "5910",                                # bad debt expense
    "5960", "5970",                        # cash count variance + misc adjustments (new)
]


def verify_coa(company_id):
    """Return a list of missing required account codes for one company.
    Empty list = healthy. Caller decides whether to flash/raise."""
    present = {a.code for a in Account.query.filter(
        Account.company_id == company_id,
        Account.code.in_(REQUIRED_ACCOUNTS),
    ).all()}
    return [code for code in REQUIRED_ACCOUNTS if code not in present]


def verify_coa_or_raise(company_id):
    """Convenience wrapper for service code that wants to fail fast."""
    missing = verify_coa(company_id)
    if missing:
        raise RuntimeError(
            f"شجرة الحسابات ناقصة لشركة #{company_id} — "
            f"الحسابات التالية مفقودة: {', '.join(missing)}. "
            f"شغّل seed_default_coa أو راجع الإدارة."
        )
