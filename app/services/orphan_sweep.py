"""MARSOUD-POS-ORPHAN-CASCADE (Abdelhamid 2026-07-22).

One-shot sweep for orphan child rows left behind by past bulk-SQL
deletes that ran BEFORE the CASCADE FK migration
(migrations/versions/a6c9f2e5b8d1). Called from `create_app()` so
prod boots into a clean state on the next deploy. Cheap — six
DELETE ... WHERE NOT IN (SELECT ...) queries touching only orphan
rows, no full-table scan cost when there's nothing to clean.

Also runs an integrity probe for the cross-tenant variant drift
(MARSOUD-POS-CROSS-TENANT-FIX): rows where
`product_variants.company_id != products.company_id`. Reports the
count via the app logger — we DO NOT auto-fix these because
guessing which of two company_ids is correct would be dangerous
(might leak data further); instead we surface the number so the
operator can review.
"""
import logging
from sqlalchemy import text

logger = logging.getLogger(__name__)


ORPHAN_QUERIES = (
    # (label, DELETE ... query). Each targets rows whose parent has
    # been deleted. Runs unconditionally on boot; no-op when empty.
    ("invoice_items",
     "DELETE FROM invoice_items WHERE invoice_id NOT IN "
     "(SELECT id FROM invoices)"),
    ("payments",
     "DELETE FROM payments WHERE invoice_id NOT IN "
     "(SELECT id FROM invoices)"),
    ("invoice_reminders_sent",
     "DELETE FROM invoice_reminders_sent WHERE invoice_id NOT IN "
     "(SELECT id FROM invoices)"),
    ("stock_balances",
     "DELETE FROM stock_balances WHERE variant_id NOT IN "
     "(SELECT id FROM product_variants)"),
    ("stock_movements",
     "DELETE FROM stock_movements WHERE variant_id NOT IN "
     "(SELECT id FROM product_variants)"),
    ("stock_lots",
     "DELETE FROM stock_lots WHERE variant_id NOT IN "
     "(SELECT id FROM product_variants)"),
)


def sweep_orphans(engine):
    """Delete any orphan child rows. Returns a dict of {table: count}
    for the rows removed. Empty dict when the DB is clean.
    """
    removed = {}
    with engine.begin() as conn:
        for label, sql in ORPHAN_QUERIES:
            try:
                r = conn.execute(text(sql))
                n = r.rowcount or 0
                if n:
                    removed[label] = n
            except Exception as e:   # noqa: BLE001
                # A missing table (e.g. fresh install without the
                # inventory migration) shouldn't stop app boot.
                logger.warning(
                    "orphan_sweep skipping %s: %s", label, e)
    if removed:
        logger.warning(
            "orphan_sweep purged rows: %s (this indicates prior "
            "bulk-delete without CASCADE — see "
            "MARSOUD-POS-ORPHAN-CASCADE)", removed)
    return removed


def probe_variant_drift(engine):
    """Log any product_variants row whose company_id doesn't match
    its parent product's company_id. Never modifies data — guessing
    which side is correct would risk further leakage. Returns the
    count so tests can assert.
    """
    with engine.begin() as conn:
        r = conn.execute(text(
            "SELECT COUNT(*) FROM product_variants pv "
            "JOIN products p ON p.id = pv.product_id "
            "WHERE pv.company_id != p.company_id"))
        n = int(r.scalar() or 0)
    if n:
        logger.critical(
            "variant_drift: %d product_variants row(s) have "
            "company_id != parent product company_id — this can "
            "leak variants into the wrong tenant's POS grid. See "
            "MARSOUD-POS-CROSS-TENANT-FIX.", n)
    return n
