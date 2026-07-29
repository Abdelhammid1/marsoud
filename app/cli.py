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
        "description": "تجارة تجزئة — 7 مستخدمين + Starter + المخزون + نقطة البيع + CRM",
        "price_monthly": 1499,
        "price_yearly": 14990,
        "modules": [
            "accounting", "sales", "purchases", "reports", "agent",
            "inventory", "pos", "crm", "hr",
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
        ],
        "quotas": {
            "users": (15, 130),
            "ai_tokens_month": (1_000_000, 80),
            "storage_bytes": (50 * 1024**3, 10),
        },
    },
]


LEGACY_TO_DEACTIVATE = ("retail", "services")


@click.command("seed-plans")
@with_appcontext
def seed_plans_command():
    """Rename + update the 3 canonical plans + seed quotas."""
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
        if plan is None:
            plan = Plan(code=cfg["code"])
            db.session.add(plan)
        plan.code = cfg["code"]
        plan.name_ar = cfg["name_ar"]
        plan.name = cfg["name"]
        plan.description = cfg["description"]
        plan.price_monthly = cfg["price_monthly"]
        plan.price_yearly = cfg["price_yearly"]
        plan.is_active = True
        plan.set_modules(cfg["modules"])
        db.session.flush()

        # Upsert quota rows.
        for key, (included, unit_price) in cfg["quotas"].items():
            qtype = quota_type_map[key]
            row = Quota.query.filter_by(
                plan_id=plan.id, quota_type=qtype).first()
            if row is None:
                row = Quota(plan_id=plan.id, quota_type=qtype)
                db.session.add(row)
            row.included_amount = included
            row.enforcement_mode = ENF_BLOCK
            row.price_per_extra_unit = unit_price
        updated.append(cfg["code"])

    # Deactivate legacy retail + services when nobody is bound.
    for code in LEGACY_TO_DEACTIVATE:
        row = Plan.query.filter_by(code=code).first()
        if not row:
            continue
        bound = Company.query.filter(
            (Company.plan_id == row.id) |
            (Company.intended_plan_id == row.id)
        ).count()
        if bound:
            click.echo(f"⚠ skipping deactivation of '{code}' — "
                        f"{bound} companies still bound")
            continue
        row.is_active = False

    db.session.commit()
    click.echo(f"✅ seeded plans: {', '.join(updated)}")


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


def register(app):
    """Wire the CLI into the Flask app. Called from app/__init__.py."""
    app.cli.add_command(seed_plans_command)
    app.cli.add_command(saas_backfill_command)
