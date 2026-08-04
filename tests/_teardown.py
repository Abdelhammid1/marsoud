"""Shared audit teardown helper.

Each audit script builds a fixture company + a mountain of children.
The company-scoped tables (accounts, invoices, journal_entries, etc.)
get wiped by iterating `db.metadata.sorted_tables` and matching on
`company_id`. But GRANDCHILDREN like invoice_items (keyed on
invoice_id, no company_id) get skipped by that loop, leaving orphans.

SQLite reuses IDs. Orphans stick around and — when the next audit
run creates a fresh invoice/BOM/work_order that happens to grab a
recycled id — the old grandchildren wire themselves back to the new
parent. The symptom is stock balances that grow by "300 + 300" across
runs, or a BOM whose `bom.lines` mysteriously has 8 lines instead of 2.

`teardown_company(cid)` here:
  1. Grabs the doomed parents' IDs (invoices, vendor_bills,
     journal_entries, boms, work_orders, products).
  2. Deletes their grandchildren by that id list.
  3. Runs the company-scoped sweep.
  4. Deletes the Company row via raw SQL — sidesteps SQLAlchemy's
     cascade attempts to null out NOT NULL FKs on tracked objects.
  5. Sweeps any residual orphans across the DB (defensive).

Every audit script should call this instead of rolling its own.
"""
from sqlalchemy import text, inspect

from app import db


# (child_table, parent_id_column, parent_table). We look up parent_ids
# with WHERE company_id = :c on parent_table, then wipe children.
_GRANDCHILD_MAP = [
    ("invoice_items",           "invoice_id",       "invoices"),
    ("payments",                "invoice_id",       "invoices"),
    ("vendor_bill_items",       "bill_id",          "vendor_bills"),
    ("vendor_bill_payments",    "bill_id",          "vendor_bills"),
    ("journal_lines",           "entry_id",         "journal_entries"),
    ("bom_lines",               "bom_id",           "bill_of_materials"),
    ("work_order_consumption",  "work_order_id",    "work_orders"),
    ("product_units",           "product_id",       "products"),
    # employee history / accruals / leave — keyed on employee_id
    ("employee_history",        "employee_id",      "employees"),
    ("employee_accruals",       "employee_id",      "employees"),
    ("leave_balances",          "employee_id",      "employees"),
    ("leave_requests",          "employee_id",      "employees"),
    ("attendance_exceptions",   "employee_id",      "employees"),
    ("payroll_lines",           "employee_id",      "employees"),
    # invoice reminders (per company via invoice)
    ("invoice_reminders_sent",  "invoice_id",       "invoices"),
]


def teardown_company(company_id):
    """Wipe every trace of `company_id` from the DB. Safe on partial
    schemas — tables that don't exist in the current DB are skipped."""
    db.session.close()
    insp = inspect(db.engine)
    live = set(insp.get_table_names())
    with db.engine.begin() as conn:
        # Grandchildren: pull parent IDs, then wipe children.
        for child, fk, parent in _GRANDCHILD_MAP:
            if child not in live or parent not in live:
                continue
            pid_rows = conn.execute(
                text(f"SELECT id FROM {parent} WHERE company_id = :c"),
                {"c": company_id},
            ).fetchall()
            if not pid_rows:
                continue
            pid_list = ",".join(str(r[0]) for r in pid_rows)
            conn.execute(text(
                f"DELETE FROM {child} WHERE {fk} IN ({pid_list})"
            ))

        # Company-scoped tables (any row where company_id = :c).
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(text(
                    f"DELETE FROM {tbl.name} WHERE company_id = :c"
                ), {"c": company_id})

        # The Company row itself.
        conn.execute(text(
            "DELETE FROM companies WHERE id = :c"
        ), {"c": company_id})

        # Belt-and-braces: sweep any residual orphans anywhere in the DB.
        _sweep_orphans(conn, live)


def _sweep_orphans(conn, live):
    """Delete rows whose FK no longer resolves. Guards the next test
    run from lingering pollution."""
    if "invoice_items" in live:
        conn.execute(text(
            "DELETE FROM invoice_items WHERE invoice_id NOT IN (SELECT id FROM invoices)"
        ))
    if "payments" in live:
        conn.execute(text(
            "DELETE FROM payments WHERE invoice_id NOT IN (SELECT id FROM invoices)"
        ))
    if "vendor_bill_items" in live:
        conn.execute(text(
            "DELETE FROM vendor_bill_items WHERE bill_id NOT IN (SELECT id FROM vendor_bills)"
        ))
    if "journal_lines" in live:
        conn.execute(text(
            "DELETE FROM journal_lines WHERE entry_id NOT IN (SELECT id FROM journal_entries)"
        ))
    if "bom_lines" in live:
        conn.execute(text(
            "DELETE FROM bom_lines WHERE bom_id NOT IN (SELECT id FROM bill_of_materials)"
        ))
    if "work_order_consumption" in live:
        conn.execute(text(
            "DELETE FROM work_order_consumption WHERE work_order_id NOT IN (SELECT id FROM work_orders)"
        ))
    if "product_units" in live:
        conn.execute(text(
            "DELETE FROM product_units WHERE product_id NOT IN (SELECT id FROM products)"
        ))
    if "stock_balances" in live:
        conn.execute(text(
            "DELETE FROM stock_balances WHERE variant_id NOT IN (SELECT id FROM product_variants)"
        ))
        # MARSOUD-STOCK-BALANCE-CASCADE — the warehouse half, never swept
        # before. stock_balances has two FKs; only one was checked.
        if "warehouses" in live:
            conn.execute(text(
                "DELETE FROM stock_balances WHERE warehouse_id NOT IN (SELECT id FROM warehouses)"
            ))
    if "stock_movements" in live:
        conn.execute(text(
            "DELETE FROM stock_movements WHERE variant_id NOT IN (SELECT id FROM product_variants)"
        ))
    # User-scoped activity/notification tables. These caused a false
    # regression in audit_daily_reports where a fresh "empty" employee
    # inherited leftover activity rows from a prior audit's actor user
    # after SQLite recycled the user_id.
    if "user_activity_log" in live:
        conn.execute(text(
            "DELETE FROM user_activity_log WHERE user_id NOT IN (SELECT id FROM users)"
        ))
    if "task_activity_logs" in live:
        conn.execute(text(
            "DELETE FROM task_activity_logs WHERE user_id NOT IN (SELECT id FROM users)"
        ))
    if "lead_activities" in live:
        conn.execute(text(
            "DELETE FROM lead_activities WHERE created_by_id NOT IN (SELECT id FROM users)"
        ))
    if "lead_status_events" in live:
        conn.execute(text(
            "DELETE FROM lead_status_events WHERE changed_by_id NOT IN (SELECT id FROM users)"
        ))
    if "notifications" in live:
        conn.execute(text(
            "DELETE FROM notifications WHERE user_id NOT IN (SELECT id FROM users)"
        ))
    if "employee_daily_reports" in live:
        conn.execute(text(
            "DELETE FROM employee_daily_reports WHERE employee_id NOT IN (SELECT id FROM employees)"
        ))
    if "stock_lots" in live:
        conn.execute(text(
            "DELETE FROM stock_lots WHERE variant_id NOT IN (SELECT id FROM product_variants)"
        ))
