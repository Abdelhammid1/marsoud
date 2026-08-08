"""MARSOUD-4-BRANCH-REPAIR (2026-08-08) — audit safety guard.

Runtime defence against the class of bug that wiped 50 attendance
exceptions + 12 check-ins from prod on 2026-08-08.

Import + install() this at the TOP of any audit script:

    import tests._audit_guard as _audit_guard  # noqa: E402
    _audit_guard.install()

After that, calling `Foo.query.delete()` on any table in
_UNSAFE_TABLES *without a WHERE clause* raises RuntimeError
instead of silently wiping every tenant's rows. A properly
company-scoped delete (`Foo.query.filter_by(company_id=X).delete()`
or any other WHERE) passes through untouched.

The guard is import-time only — production code paths never
import this module, so the app itself is unaffected.

New unsafe tables are cheap to add: extend _UNSAFE_TABLES.
"""
import sqlalchemy.orm as _orm


_UNSAFE_TABLES = {
    # Attendance — the 2026-08-08 incident
    "attendance_checkins",
    "attendance_exceptions",
    "late_permission_requests",
    "attendance_violation_policies",
    # Any cross-tenant bulk delete on these would be a disaster
    "companies",
    "users",
    "tasks",
    "invoices",
    "invoice_items",
    "payments",
    "vendor_bills",
    "vendor_bill_items",
    "journal_entries",
    "journal_lines",
    "customers",
    "vendors",
    "employees",
    "payroll_runs",
    "payroll_lines",
    # Attendance-adjacent audit tables that also had unscoped calls
    "attendance_policies",
    "leave_requests",
    "leave_balances",
    # AI + platform audit — same risk profile
    "ai_token_usage",
    "platform_audit_logs",
    "platform_errors",
    "platform_cron_runs",
}


_ORIGINAL_DELETE = _orm.Query.delete
_INSTALLED = False


def _guarded_delete(self, *args, **kwargs):
    """Refuse a WHERE-less bulk delete on any table in _UNSAFE_TABLES.
    Otherwise defer to SQLAlchemy's original delete().
    """
    tbl = None
    try:
        # column_descriptions is the stable public API for reading
        # what the Query targets — a list of dicts, each with the
        # entity class under key 'entity'. _entity_from_pre_ent_zero
        # is internal and changed shape between SA versions.
        cols = getattr(self, "column_descriptions", None) or []
        for desc in cols:
            cls = desc.get("entity") if isinstance(desc, dict) else None
            tn = getattr(cls, "__tablename__", None)
            if tn:
                tbl = tn
                break
    except Exception:
        # Best-effort — if we can't identify the table, let SQLAlchemy
        # decide. Failing safe would break too much; the guard is
        # opt-in per-file anyway.
        pass

    if tbl in _UNSAFE_TABLES:
        # A Query with no WHERE has an empty _where_criteria tuple.
        # Anything at all in there means the caller filtered.
        criteria = getattr(self, "_where_criteria", ()) or ()
        if len(criteria) == 0:
            raise RuntimeError(
                f"AUDIT SAFETY — refusing to bulk-delete {tbl!r} "
                f"without a WHERE. Add "
                f".filter_by(company_id=...) or an explicit filter "
                f"before .delete(). "
                f"(This guard exists because a prior audit wiped "
                f"prod tenants' rows via Foo.query.delete().)"
            )

    return _ORIGINAL_DELETE(self, *args, **kwargs)


def install():
    """Idempotent — safe to call more than once (audit scripts may
    each install at import time; second call is a no-op)."""
    global _INSTALLED
    if _INSTALLED:
        return
    _orm.Query.delete = _guarded_delete
    _INSTALLED = True


def uninstall():
    """Restore the original .delete — used only from the audit runner's
    finally block if a test wants to disable the guard temporarily."""
    global _INSTALLED
    if not _INSTALLED:
        return
    _orm.Query.delete = _ORIGINAL_DELETE
    _INSTALLED = False
