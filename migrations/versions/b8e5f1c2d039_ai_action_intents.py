"""MARSOUD-AI-ACTION-FRAMEWORK-01 — ai_action_intents table.

Foundation for the Confirm-to-Execute framework. Every write the AI
Accountant wants to perform lands here in PENDING first; the human
confirms from the chat before the executor fires.

Revision ID: b8e5f1c2d039
Revises: a7d3f8c19e42
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa


revision = "b8e5f1c2d039"
down_revision = "a7d3f8c19e42"
branch_labels = None
depends_on = None


TABLE = "ai_action_intents"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def upgrade():
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer,
                   sa.ForeignKey("companies.id",
                                 name="fk_ai_intent_company"),
                   nullable=False, index=True),
        sa.Column("action_type", sa.String(60), nullable=False,
                   index=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False,
                   server_default="PENDING", index=True),
        sa.Column("proposed_by", sa.String(60), nullable=False,
                   server_default="ai_agent"),
        sa.Column("proposed_by_user_id", sa.Integer,
                   sa.ForeignKey("users.id",
                                 name="fk_ai_intent_proposer"),
                   nullable=True, index=True),
        sa.Column("confirmed_by_user_id", sa.Integer,
                   sa.ForeignKey("users.id",
                                 name="fk_ai_intent_confirmer"),
                   nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                   server_default=sa.func.current_timestamp()),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
    )


def downgrade():
    if _has_table(TABLE):
        op.drop_table(TABLE)
