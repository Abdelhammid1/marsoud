"""MARSOUD-SAAS-CUSTOMER-PHONE (2026-08-18) — backfill
`customers.phone` for the SaaS-mirror rows that were created with
`phone=NULL` by the historical `ensure_saas_customer` bug.

Every Company that bought a plan got a Customer row in Manasty's
own books (the SaaS-side "tenants are customers of the provider"
model). The `phone` column on that mirror was hard-coded to NULL
by `app/services/saas_billing.py:91-97`, even though both
`Company.phone` and the owner's `User.phone` were correctly
persisted at signup. The code fix (this branch) closes the leak
for future signups; this migration heals the existing rows.

Join key: `companies.saas_customer_id` — the 1:1 FK the SaaS
billing module already tracks. Zero fuzzy matching, zero risk of
touching a legitimate walk-in customer that a tenant created
by hand.

Priority mirrors the code fix: `COALESCE(company.phone, owner_phone)`
(business-facing number preferred, owner's personal as fallback).
Never overwrites a non-NULL phone — the WHERE clause makes this
strictly a one-way NULL → set transition, so a super-admin who
corrected a Customer.phone by hand doesn't get their edit blown
away.

Idempotent. Safe to re-run. Safe on partial-run histories.

Revision ID: n1o4p7q0r3s6
Revises: 63492bd67619
Create Date: 2026-08-18
"""
from alembic import op
import sqlalchemy as sa


revision = "n1o4p7q0r3s6"
down_revision = "63492bd67619"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table, col):
    try:
        insp = _inspector()
        if table not in insp.get_table_names():
            return False
        return any(c["name"] == col for c in insp.get_columns(table))
    except Exception:
        return False


def upgrade():
    # Defensive — every column referenced below must exist. If any
    # is missing (unusual partial-migration state) skip the whole
    # backfill rather than raise.
    for tbl, col in [
        ("customers", "phone"),
        ("companies", "saas_customer_id"),
        ("companies", "phone"),
        ("users", "phone"),
        ("user_companies", "role"),
    ]:
        if not _has_column(tbl, col):
            return

    bind = op.get_bind()
    dialect = bind.dialect.name

    # PostgreSQL and MySQL support UPDATE ... FROM / JOIN in
    # different syntaxes; SQLite doesn't support UPDATE FROM at
    # all until 3.33. Correlated subquery works on every dialect.
    #
    # The subquery picks ONE phone per (tenant → customer)
    # pairing: preferring `owner`, then `admin`, then any user;
    # within each role tier we take the lowest user_id for
    # determinism. Mirrors the priority `_tenant_owner_phone`
    # uses in Python.
    if dialect == "sqlite":
        # SQLite: correlated subquery form. Two selects (COALESCE
        # on the outer expression) because SQLite before 3.33
        # doesn't allow UPDATE FROM.
        bind.execute(sa.text("""
            UPDATE customers
               SET phone = (
                     SELECT COALESCE(
                              co.phone,
                              (SELECT u.phone
                                 FROM users u
                                 JOIN user_companies uc
                                   ON uc.user_id = u.id
                                WHERE uc.company_id = co.id
                                ORDER BY (CASE
                                          WHEN uc.role='owner' THEN 0
                                          WHEN uc.role='admin' THEN 1
                                          ELSE 2 END), u.id
                                LIMIT 1))
                       FROM companies co
                      WHERE co.saas_customer_id = customers.id
                      LIMIT 1)
             WHERE customers.phone IS NULL
               AND EXISTS (
                     SELECT 1 FROM companies co
                      WHERE co.saas_customer_id = customers.id
                        AND (co.phone IS NOT NULL
                             OR EXISTS (
                                  SELECT 1 FROM users u
                                    JOIN user_companies uc
                                      ON uc.user_id = u.id
                                   WHERE uc.company_id = co.id
                                     AND u.phone IS NOT NULL)))
        """))
    else:
        # PostgreSQL / MySQL: UPDATE ... FROM ... JOIN form. Uses
        # a LEFT JOIN so companies without an owner still pull the
        # company.phone if it's set.
        bind.execute(sa.text("""
            UPDATE customers c
               SET phone = COALESCE(co.phone, owner.phone)
              FROM companies co
              LEFT JOIN LATERAL (
                    SELECT u.phone
                      FROM users u
                      JOIN user_companies uc ON uc.user_id = u.id
                     WHERE uc.company_id = co.id
                     ORDER BY (CASE
                               WHEN uc.role='owner' THEN 0
                               WHEN uc.role='admin' THEN 1
                               ELSE 2 END), u.id
                     LIMIT 1
              ) AS owner ON TRUE
             WHERE co.saas_customer_id = c.id
               AND c.phone IS NULL
               AND COALESCE(co.phone, owner.phone) IS NOT NULL
        """))


def downgrade():
    # Non-destructive by design — a downgrade cannot know which
    # rows this migration wrote (they were NULL before), so the
    # only safe rollback is a no-op. Manual correction stays in
    # place.
    pass
