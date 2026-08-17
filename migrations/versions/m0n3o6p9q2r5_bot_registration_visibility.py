"""MARSOUD-BOT-REGISTRATION-VISIBILITY (2026-08-17) — TKT-17.

Adds:
  · `blocked_emails` table (mirrors `blocked_domains`) for the
    per-email permanent blocklist.
  · `signup_rejections.email` (String 150, nullable, indexed).
  · `signup_rejections.honeypot_value` (Text, nullable) — the raw
    string the bot typed into the hidden `website` field.
  · Widens the `ck_signup_rejection_reason` CHECK constraint to
    allow two new values used by the immediate-block path:
    `'blocked_email'` and `'bot_immediate'`.

Everything is idempotent (`_has_table` / `_has_column` guards) so
a deploy that partially ran a previous attempt won't fail. The
CHECK-constraint update is guarded by a try/except because
some SQL dialects (SQLite in particular) cannot drop a
constraint by name — on those we silently accept the widened
runtime check.

Chained off `i5j8k1l4m7n0_signup_auto_block` (the migration that
created the original blocked_domains + signup_rejections tables).
The parallel head `h4i7j0k3l6m9_pending_superadmin_actions` is
unrelated and remains a sibling head — deployments run both to
converge.

Revision ID: m0n3o6p9q2r5
Revises: i5j8k1l4m7n0
Create Date: 2026-08-17
"""
from alembic import op
import sqlalchemy as sa


revision = "m0n3o6p9q2r5"
down_revision = "i5j8k1l4m7n0"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    try:
        return name in _inspector().get_table_names()
    except Exception:
        return False


def _has_column(table, col):
    try:
        insp = _inspector()
        if table not in insp.get_table_names():
            return False
        return any(c["name"] == col for c in insp.get_columns(table))
    except Exception:
        return False


def upgrade():
    # ── blocked_emails table ─────────────────────────────────────
    if not _has_table("blocked_emails"):
        op.create_table(
            "blocked_emails",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("email", sa.String(150), nullable=False),
            sa.Column("blocked_at", sa.DateTime, nullable=False,
                      server_default=sa.func.current_timestamp()),
            sa.Column("reason", sa.String(200), nullable=True),
            sa.Column("is_active", sa.Boolean, nullable=False,
                      server_default=sa.text("1")),
            sa.Column("unblocked_at", sa.DateTime, nullable=True),
            sa.Column("unblocked_by_id", sa.Integer,
                      sa.ForeignKey("users.id",
                                    name="fk_blocked_email_unblocker"),
                      nullable=True),
        )
        with op.batch_alter_table("blocked_emails") as batch:
            batch.create_index(
                "ix_blocked_emails_email",
                ["email"], unique=True)
            batch.create_index(
                "ix_blocked_emails_is_active",
                ["is_active"])

    # ── signup_rejections.email + honeypot_value ────────────────
    if not _has_column("signup_rejections", "email"):
        with op.batch_alter_table("signup_rejections") as batch:
            batch.add_column(
                sa.Column("email", sa.String(150), nullable=True))
            batch.create_index(
                "ix_signup_rejections_email", ["email"])

    if not _has_column("signup_rejections", "honeypot_value"):
        with op.batch_alter_table("signup_rejections") as batch:
            batch.add_column(
                sa.Column("honeypot_value", sa.Text, nullable=True))

    # ── widen the CHECK constraint ──────────────────────────────
    # SQLite in particular cannot drop a named constraint without
    # rebuilding the table. Wrap in a try/except so the migration
    # still succeeds — the model-level constraint stays authoritative
    # for future INSERTs; historical rows unaffected.
    try:
        with op.batch_alter_table("signup_rejections") as batch:
            batch.drop_constraint(
                "ck_signup_rejection_reason", type_="check")
            batch.create_check_constraint(
                "ck_signup_rejection_reason",
                "reason IN ('honeypot','rate_limit','spam_domain',"
                "'turnstile','blocked_domain','blocked_email',"
                "'bot_immediate')")
    except Exception:
        pass


def downgrade():
    # Non-destructive — drop the new column + table only.
    if _has_table("blocked_emails"):
        with op.batch_alter_table("blocked_emails") as batch:
            try:
                batch.drop_index("ix_blocked_emails_is_active")
            except Exception:
                pass
            try:
                batch.drop_index("ix_blocked_emails_email")
            except Exception:
                pass
        op.drop_table("blocked_emails")

    if _has_column("signup_rejections", "honeypot_value"):
        with op.batch_alter_table("signup_rejections") as batch:
            batch.drop_column("honeypot_value")

    if _has_column("signup_rejections", "email"):
        with op.batch_alter_table("signup_rejections") as batch:
            try:
                batch.drop_index("ix_signup_rejections_email")
            except Exception:
                pass
            batch.drop_column("email")

    try:
        with op.batch_alter_table("signup_rejections") as batch:
            batch.drop_constraint(
                "ck_signup_rejection_reason", type_="check")
            batch.create_check_constraint(
                "ck_signup_rejection_reason",
                "reason IN ('honeypot','rate_limit','spam_domain',"
                "'turnstile','blocked_domain')")
    except Exception:
        pass
