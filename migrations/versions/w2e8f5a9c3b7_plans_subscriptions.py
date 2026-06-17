"""MARSOUD-57.2 + 57.3 — Plans system + Subscription tracking

New tables:
  plans                     id, code, name_ar, name, description,
                            price_monthly, price_yearly,
                            allowed_modules (JSON-text),
                            is_active, created_at
  subscription_reminders_sent  company_id, threshold_days, sent_at
                               (prevents duplicate reminders, like
                               InvoiceReminderSent for invoices)

Added to companies:
  plan_id                   FK → plans.id, nullable (backfilled to متكامل)
  subscription_started_at   DateTime, nullable
  subscription_expires_at   DateTime, nullable (backfilled +100y)

Seeds 3 initial plans (basic, professional, enterprise) + assigns
enterprise to every existing active company so nothing locks
suddenly. The super-admin can change assignments later.

Revision ID: w2e8f5a9c3b7
Revises: v1c4d7a2b8e5
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa
import json
from datetime import datetime, timedelta

revision = 'w2e8f5a9c3b7'
down_revision = 'v1c4d7a2b8e5'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def _has_table(name):
    insp = sa.inspect(op.get_bind())
    return name in insp.get_table_names()


# Module catalog — what each plan's allowed_modules can contain.
# A module here is a coarse-grained gate over many permissions.
# Maps in app/services/plan_gating.py do the action→module lookup.
MODULES_BASIC = ["accounting", "sales", "reports", "settings"]
MODULES_PRO = MODULES_BASIC + ["pos", "inventory", "purchases"]
MODULES_ENTERPRISE = MODULES_PRO + ["crm", "hr", "agent"]


def upgrade():
    conn = op.get_bind()

    # ── plans table ─────────────────────────────────────────────────
    if not _has_table("plans"):
        op.create_table(
            "plans",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("code", sa.String(40), nullable=False, unique=True),
            sa.Column("name_ar", sa.String(120), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("price_monthly", sa.Numeric(10, 2), nullable=True),
            sa.Column("price_yearly", sa.Numeric(10, 2), nullable=True),
            sa.Column("allowed_modules", sa.Text(), nullable=False,
                      server_default=sa.text("'[]'")),
            sa.Column("is_active", sa.Boolean(), nullable=False,
                      server_default=sa.text("1")),
            sa.Column("created_at", sa.DateTime(), nullable=True,
                      server_default=sa.func.current_timestamp()),
        )

    # ── subscription_reminders_sent table ───────────────────────────
    if not _has_table("subscription_reminders_sent"):
        op.create_table(
            "subscription_reminders_sent",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"), nullable=False, index=True),
            sa.Column("threshold_days", sa.Integer(), nullable=False),
            sa.Column("expires_at_when_sent", sa.DateTime(), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.current_timestamp()),
        )

    # ── companies columns ───────────────────────────────────────────
    if not _has_col("companies", "plan_id"):
        with op.batch_alter_table("companies", schema=None) as batch:
            batch.add_column(sa.Column("plan_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_companies_plan_id", "plans", ["plan_id"], ["id"],
            )
    if not _has_col("companies", "subscription_started_at"):
        with op.batch_alter_table("companies", schema=None) as batch:
            batch.add_column(sa.Column("subscription_started_at",
                                        sa.DateTime(), nullable=True))
    if not _has_col("companies", "subscription_expires_at"):
        with op.batch_alter_table("companies", schema=None) as batch:
            batch.add_column(sa.Column("subscription_expires_at",
                                        sa.DateTime(), nullable=True))

    # ── seed the 3 initial plans, idempotent ────────────────────────
    plans_to_seed = [
        ("basic", "أساسي", "Basic", "المحاسبة + الفواتير + التقارير الأساسية",
         99, 990, MODULES_BASIC),
        ("professional", "احترافي", "Professional",
         "أساسي + نقطة البيع + المخزون + المشتريات",
         249, 2490, MODULES_PRO),
        ("enterprise", "متكامل", "Enterprise",
         "احترافي + CRM + الموارد البشرية + المحاسب الذكي",
         499, 4990, MODULES_ENTERPRISE),
    ]
    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    plan_ids = {}
    for code, name_ar, name, desc, m, y, mods in plans_to_seed:
        existing = conn.execute(sa.text(
            "SELECT id FROM plans WHERE code = :code"
        ), {"code": code}).fetchone()
        if existing:
            plan_ids[code] = existing[0]
        else:
            res = conn.execute(sa.text(
                "INSERT INTO plans (code, name_ar, name, description, "
                "price_monthly, price_yearly, allowed_modules, is_active, "
                "created_at) VALUES (:code, :name_ar, :name, :desc, :m, :y, "
                ":mods, 1, :now)"
            ), {"code": code, "name_ar": name_ar, "name": name, "desc": desc,
                "m": m, "y": y, "mods": json.dumps(mods), "now": now})
            plan_ids[code] = res.lastrowid

    # ── backfill every active company → enterprise plan + 1-month expiry
    # FIX (abdelhamid): the original migration used 100 years which produced
    # year 2126 dates that look like data corruption. The right default for
    # new sign-ups is one month from the activation date.
    enterprise_id = plan_ids.get("enterprise")
    one_month = (datetime.utcnow() + timedelta(days=30)).isoformat(
        sep=" ", timespec="seconds"
    )
    if enterprise_id:
        conn.execute(sa.text(
            "UPDATE companies SET plan_id = :pid WHERE plan_id IS NULL"
        ), {"pid": enterprise_id})
    conn.execute(sa.text(
        "UPDATE companies SET subscription_started_at = :now "
        "WHERE subscription_started_at IS NULL"
    ), {"now": now})
    conn.execute(sa.text(
        "UPDATE companies SET subscription_expires_at = :ex "
        "WHERE subscription_expires_at IS NULL"
    ), {"ex": one_month})


def downgrade():
    if _has_col("companies", "subscription_expires_at"):
        with op.batch_alter_table("companies", schema=None) as batch:
            batch.drop_column("subscription_expires_at")
    if _has_col("companies", "subscription_started_at"):
        with op.batch_alter_table("companies", schema=None) as batch:
            batch.drop_column("subscription_started_at")
    if _has_col("companies", "plan_id"):
        with op.batch_alter_table("companies", schema=None) as batch:
            try:
                batch.drop_constraint("fk_companies_plan_id", type_="foreignkey")
            except Exception:
                pass
            batch.drop_column("plan_id")
    if _has_table("subscription_reminders_sent"):
        op.drop_table("subscription_reminders_sent")
    if _has_table("plans"):
        op.drop_table("plans")
