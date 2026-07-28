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


def register(app):
    """Wire the CLI into the Flask app. Called from app/__init__.py."""
    app.cli.add_command(seed_plans_command)
