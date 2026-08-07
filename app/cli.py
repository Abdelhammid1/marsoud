"""MARSOUD-PLANS-COMPLETE (Abdelhamid 2026-07-22) — Flask CLI.

`flask seed-plans` renames/updates the three canonical plans
(Starter / Growth / Pro) with the exact prices + module lists +
quota values from the ticket. Idempotent — reruns are safe. Also
deactivates the legacy retail + services plans when no company
is bound to them.
"""
import click
from flask.cli import with_appcontext


# Canonical plan configuration from Abdelhamid's ticket.
PLAN_SEED = [
    {
        "old_code": "basic",
        "code": "starter",
        "name_ar": "Starter",
        "name": "Starter",
        "description": "خدمي / فريلانسر — 3 مستخدمين + المحاسبة + المبيعات + التقارير + الوكيل الذكي",
        "price_monthly": 799,
        "price_yearly": 7990,
        "modules": [
            "accounting", "sales", "purchases", "reports", "agent",
            "inventory", "pos",
        ],
        "quotas": {
            "users": (3, 150),                    # 150 EGP/extra user
            "ai_tokens_month": (300_000, 100),    # 100 EGP/100k
            "storage_bytes": (2 * 1024**3, 15),   # 15 EGP/extra GB
        },
    },
    {
        "old_code": "professional",
        "code": "growth",
        "name_ar": "Growth",
        "name": "Growth",
        # MARSOUD-PLAN-BUNDLE-FIXES-01 (2026-08-07) — HR removed from
        # Growth to match the plan-comparison marketing matrix (HR is
        # Pro-only). Existing Growth tenants that were previously
        # using HR keep their data (payroll runs / employees /
        # attendance rows stay on disk); the module is just no
        # longer exposed. Sales/support convert them to Pro if the
        # tenant still needs HR features.
        "description": "تجارة تجزئة — 7 مستخدمين + Starter + CRM + إدارة المهام والمشاريع",
        "price_monthly": 1499,
        "price_yearly": 14990,
        "modules": [
            "accounting", "sales", "purchases", "reports", "agent",
            "inventory", "pos", "crm",
        ],
        "quotas": {
            "users": (7, 150),
            "ai_tokens_month": (600_000, 90),
            "storage_bytes": (10 * 1024**3, 12),
        },
    },
    {
        "old_code": "enterprise",
        "code": "pro",
        "name_ar": "Pro",
        "name": "Pro",
        "description": "وكالات / مصانع — 15 مستخدم + كل الموديولات + HR + التصنيع",
        "price_monthly": 2799,
        "price_yearly": 27990,
        "modules": [
            "accounting", "sales", "purchases", "reports", "agent",
            "inventory", "pos", "crm",
            "hr", "employee_reports", "manufacturing",
            # MARSOUD-EVALUATIONS-PRO-GATING (Batch 5 Ticket 6, 2026-07-29).
            "evaluations",
            # MARSOUD-CASH-CUSTODY-01 (2026-08-07) — Pro tier only.
            # Custody is an operational-control feature aligned with
            # organizations that already run payroll + fixed assets +
            # evaluations. Growth stays lean; super-admin can flip it
            # on for a specific Growth company via /admin/plans.
            "cash_custody",
            # MARSOUD-INSIGHTS-AGENT-PROFESSIONAL followup — same rationale.
            # Was missed in T9 (plan gate blocked the sidebar row).
            "insights",
        ],
        "quotas": {
            "users": (15, 130),
            "ai_tokens_month": (1_000_000, 80),
            "storage_bytes": (50 * 1024**3, 10),
        },
    },
]


LEGACY_TO_DEACTIVATE = ("retail", "services")


def sync_plans_from_seed():
    """MARSOUD-PLAN-BUNDLE-FIXES-01 (2026-08-07) — pure function that
    rewrites Plan + Quota rows from PLAN_SEED. Idempotent; safe to
    call at boot to auto-heal drift between the seed and the DB.

    Returns a dict summary:
        {
          "updated":     [<plan_code>, ...]  — plans that changed on disk
          "deactivated": [<plan_code>, ...]  — legacy plans just flipped inactive
          "skipped":     [(<plan_code>, <reason>), ...]
        }

    Splits into (`updated` vs no-op) by comparing what would be
    written to what's already there BEFORE mutating — so the boot
    shim can decide whether to log "auto-healed" or stay quiet.
    """
    from app import db
    from app.models import (
        Plan, Company, Quota,
        QUOTA_USERS, QUOTA_AI_TOKENS_MONTH, QUOTA_STORAGE_BYTES,
        ENF_BLOCK,
    )

    quota_type_map = {
        "users": QUOTA_USERS,
        "ai_tokens_month": QUOTA_AI_TOKENS_MONTH,
        "storage_bytes": QUOTA_STORAGE_BYTES,
    }

    updated = []
    for cfg in PLAN_SEED:
        # Prefer the row already at the new code (rerun); else rename
        # from the legacy code.
        plan = (Plan.query.filter_by(code=cfg["code"]).first()
                or Plan.query.filter_by(code=cfg["old_code"]).first())
        is_new = plan is None
        if is_new:
            plan = Plan(code=cfg["code"])
            db.session.add(plan)

        # Drift detection before writing — compare seed to current
        # row so the boot shim only logs when something actually
        # changed.
        drift = is_new or (
            plan.code != cfg["code"]
            or plan.name_ar != cfg["name_ar"]
            or plan.name != cfg["name"]
            or plan.description != cfg["description"]
            or float(plan.price_monthly or 0) != float(cfg["price_monthly"])
            or float(plan.price_yearly or 0) != float(cfg["price_yearly"])
            or set(plan.modules or []) != set(cfg["modules"])
            or not plan.is_active
        )

        plan.code = cfg["code"]
        plan.name_ar = cfg["name_ar"]
        plan.name = cfg["name"]
        plan.description = cfg["description"]
        plan.price_monthly = cfg["price_monthly"]
        plan.price_yearly = cfg["price_yearly"]
        plan.is_active = True
        plan.set_modules(cfg["modules"])
        db.session.flush()

        # Upsert quota rows. Any quota row diff also counts as drift.
        for key, (included, unit_price) in cfg["quotas"].items():
            qtype = quota_type_map[key]
            row = Quota.query.filter_by(
                plan_id=plan.id, quota_type=qtype).first()
            if row is None:
                drift = True
                row = Quota(plan_id=plan.id, quota_type=qtype)
                db.session.add(row)
            elif (row.included_amount != included
                  or row.enforcement_mode != ENF_BLOCK
                  or float(row.price_per_extra_unit or 0)
                      != float(unit_price)):
                drift = True
            row.included_amount = included
            row.enforcement_mode = ENF_BLOCK
            row.price_per_extra_unit = unit_price

        if drift:
            updated.append(cfg["code"])

    # Deactivate legacy retail + services when nobody is bound.
    deactivated = []
    skipped = []
    for code in LEGACY_TO_DEACTIVATE:
        row = Plan.query.filter_by(code=code).first()
        if not row:
            continue
        bound = Company.query.filter(
            (Company.plan_id == row.id) |
            (Company.intended_plan_id == row.id)
        ).count()
        if bound:
            skipped.append(
                (code, f"{bound} companies still bound"))
            continue
        if row.is_active:
            row.is_active = False
            deactivated.append(code)

    db.session.commit()
    return {"updated": updated, "deactivated": deactivated,
            "skipped": skipped}


@click.command("seed-plans")
@with_appcontext
def seed_plans_command():
    """Rename + update the 3 canonical plans + seed quotas."""
    summary = sync_plans_from_seed()
    for code, reason in summary["skipped"]:
        click.echo(f"⚠ skipping deactivation of '{code}' — {reason}")
    if summary["updated"]:
        click.echo(f"✅ seeded plans: {', '.join(summary['updated'])}")
    else:
        click.echo("✅ plans already in sync — no changes")
    if summary["deactivated"]:
        click.echo(f"↓ deactivated legacy plans: "
                    f"{', '.join(summary['deactivated'])}")


@click.command("saas-backfill")
@click.option("--dry-run", is_flag=True,
              help="Print what WOULD be created without touching the DB.")
@with_appcontext
def saas_backfill_command(dry_run):
    """MARSOUD-SAAS-BILLING-BACKFILL-01 (Batch 6 Ticket 2,
    2026-07-29) — create a first SaaS invoice for every OLD
    company that already has a chosen plan but was registered
    BEFORE the SaaS billing feature shipped.

    Idempotent — safe to re-run. Per-company:
      · If no intended_plan_id → SKIP.
      · If subscription_frequency is NULL → force to MONTHLY.
      · Calls saas_billing.create_first_invoice() which itself
        returns the existing invoice if one is already
        outstanding (so re-running doesn't spam duplicates).
    """
    from app import db
    from app.models import Company
    from app.services import saas_billing as _sb

    companies = (Company.query
                    .filter(Company.deleted_at.is_(None),
                              Company.intended_plan_id.isnot(None))
                    .order_by(Company.id)
                    .all())

    created = 0
    skipped = 0
    errored = 0
    error_list = []
    freq_forced = 0

    for c in companies:
        try:
            if not c.subscription_frequency:
                if not dry_run:
                    c.subscription_frequency = "MONTHLY"
                freq_forced += 1
            if dry_run:
                # In dry-run just count what we'd try to create.
                # A company with an outstanding SaaS invoice already
                # counts as "skip".
                from app.models import Invoice, InvoiceStatus
                existing = None
                if c.saas_customer_id:
                    existing = Invoice.query.filter_by(
                        customer_id=c.saas_customer_id,
                        source="SAAS_BILLING",
                    ).filter(Invoice.status.in_([
                        InvoiceStatus.DRAFT, InvoiceStatus.SENT,
                        InvoiceStatus.PARTIALLY_PAID,
                        InvoiceStatus.OVERDUE,
                    ])).first()
                if existing:
                    skipped += 1
                    click.echo(f"  SKIP  #{c.id} {c.name} "
                                f"(has invoice #{existing.number})")
                else:
                    created += 1
                    click.echo(f"  WOULD-CREATE  #{c.id} {c.name}")
            else:
                before = c.saas_customer_id
                inv = _sb.create_first_invoice(c)
                # If create_first_invoice returned an EXISTING
                # invoice, that's a skip. Otherwise it created one.
                # We detect this via marker on internal_notes: a
                # freshly-created invoice has no ";post_payment=1".
                # Simpler heuristic: check invoice.created_at was
                # in this last minute.
                if inv is None:
                    skipped += 1
                    click.echo(f"  SKIP  #{c.id} {c.name} "
                                f"(no plan resolved)")
                else:
                    # Use idempotency check: outstanding existing
                    # invoice → skip; anything else → created.
                    from datetime import datetime as _dt, timedelta
                    if inv.created_at and (
                        _dt.utcnow() - inv.created_at) < timedelta(
                            minutes=1):
                        created += 1
                        click.echo(f"  CREATED  #{c.id} {c.name} "
                                    f"→ invoice {inv.number}")
                    else:
                        skipped += 1
                        click.echo(f"  SKIP  #{c.id} {c.name} "
                                    f"(already has invoice {inv.number})")
                db.session.commit()
        except Exception as e:  # noqa: BLE001
            errored += 1
            error_list.append((c.id, c.name, str(e)))
            db.session.rollback()
            click.echo(f"  ERROR  #{c.id} {c.name}: {e}")

    click.echo("")
    click.echo("─" * 60)
    click.echo(f"  Companies scanned:     {len(companies)}")
    click.echo(f"  Invoices created:      {created}")
    click.echo(f"  Skipped (had one):     {skipped}")
    click.echo(f"  Errored:               {errored}")
    click.echo(f"  Frequency forced=MONTHLY: {freq_forced}")
    click.echo("─" * 60)
    if error_list:
        click.echo("\nErrors:")
        for cid, name, err in error_list:
            click.echo(f"  #{cid} {name}: {err}")
    if dry_run:
        click.echo("\n(dry-run — no writes)")


@click.command("saas-backfill-ledger")
@click.option("--dry-run", is_flag=True,
              help="List the orphan invoices without posting.")
@click.option("--yes", is_flag=True,
              help="Skip the pre-flight confirmation prompt.")
@with_appcontext
def saas_backfill_ledger_command(dry_run, yes):
    """MARSOUD-SAAS-INVOICE-LEDGER-01 (Batch 6 Ticket 6,
    2026-07-29) — one-shot script that posts a journal entry
    for every SaaS invoice (source='SAAS_BILLING') that lacks
    one. Uses the invoice's ORIGINAL issue_date as the entry
    date, so historical monthly reports don't shift.

    Cross-tenant: scans every company. Idempotent — an invoice
    that already has a JE is skipped. Safe to re-run.
    """
    from app import db
    from app.models import Invoice, InvoiceStatus
    from app.services.invoicing import post_invoice_to_ledger
    from sqlalchemy import text

    # Find orphan SaaS invoices — status != DRAFT (drafts are
    # provisional, they should NOT post to the ledger).
    rows = db.session.execute(text(
        "SELECT i.id, i.company_id, i.number, i.issue_date, "
        "       i.total, i.status "
        "FROM invoices i "
        "WHERE i.source = 'SAAS_BILLING' "
        "  AND i.status != 'DRAFT' "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM journal_entries je "
        "    WHERE je.source_type = 'invoice' "
        "      AND je.source_id = i.id"
        "  ) "
        "ORDER BY i.company_id, i.issue_date, i.id"
    )).fetchall()

    click.echo("─" * 60)
    click.echo(f"  Orphan SaaS invoices: {len(rows)}")
    click.echo("─" * 60)
    by_company = {}
    total_amount = 0.0
    for r in rows:
        by_company.setdefault(r[1], []).append(r)
        total_amount += float(r[4] or 0)
    for cid, invs in by_company.items():
        subtotal = sum(float(r[4] or 0) for r in invs)
        click.echo(f"  Company #{cid}: {len(invs)} invoices, "
                    f"total = {subtotal:,.2f}")
        for r in invs:
            click.echo(f"    {r[2]}  {r[3]}  {float(r[4] or 0):,.2f}  {r[5]}")
    click.echo("─" * 60)
    click.echo(f"  Grand total: {total_amount:,.2f}")
    click.echo("─" * 60)

    if not rows:
        click.echo("  Nothing to do — no orphan SaaS invoices.")
        return

    if dry_run:
        click.echo("\n(dry-run — no writes)")
        return

    if not yes:
        if not click.confirm(
                "\nPost journal entries for the invoices above?"):
            click.echo("aborted.")
            return

    posted = 0
    errored = 0
    error_list = []
    for r in rows:
        inv = db.session.get(Invoice, r[0])
        if inv is None:
            continue
        try:
            entry = post_invoice_to_ledger(
                inv, created_by=inv.created_by_id)
            db.session.commit()
            posted += 1
            click.echo(f"  POSTED  {inv.number}  → JE {entry.number}")
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            errored += 1
            error_list.append((inv.number, str(e)))
            click.echo(f"  ERROR  {inv.number}: {e}")

    click.echo("")
    click.echo("─" * 60)
    click.echo(f"  Posted:  {posted}")
    click.echo(f"  Errored: {errored}")
    click.echo("─" * 60)
    if error_list:
        click.echo("\nErrors:")
        for num, err in error_list:
            click.echo(f"  {num}: {err}")


def register(app):
    """Wire the CLI into the Flask app. Called from app/__init__.py."""
    app.cli.add_command(seed_plans_command)
    app.cli.add_command(saas_backfill_command)
    app.cli.add_command(saas_backfill_ledger_command)
