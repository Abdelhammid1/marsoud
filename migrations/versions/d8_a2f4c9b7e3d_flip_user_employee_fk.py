"""MARSOUD-MC-EMPLOYEE — flip User↔Employee FK to support multi-company.

`users.employee_id` was a single scalar FK that couldn't represent a user
who owns multiple companies with a separate Employee row per company.
Last-write-wins overwrites broke /my/account, payslip access, and
/hr_ss/ bucketing in every company except the most recently created one.

This migration flips the direction:
  - drops   users.employee_id
  - adds    employees.user_id     (nullable, indexed, FK → users.id)
  - adds    UNIQUE(company_id, user_id) on employees
  - backfills employees.user_id by matching users.email (LOWER)
    within the same company_id

Downgrade re-creates users.employee_id and back-copies one row per user
(the earliest employee id for that user) — the pre-fix behavior.

Revision ID: d8_a2f4c9b7e3d
Revises: d7_c8a4b2f7e5d
"""
from alembic import op
import sqlalchemy as sa


revision = "d8_a2f4c9b7e3d"
down_revision = "d7_c8a4b2f7e5d"
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    bind = op.get_bind()

    # ── employees.user_id ────────────────────────────────────────────
    if not _has_col("employees", "user_id"):
        with op.batch_alter_table("employees") as batch:
            batch.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_employees_user_id_users",
                "users", ["user_id"], ["id"], ondelete="SET NULL",
            )
            batch.create_index(
                "ix_employees_user_id", ["user_id"],
            )
            batch.create_unique_constraint(
                "uq_employees_company_user",
                ["company_id", "user_id"],
            )

    # ── Backfill: match on lower(email) within same company ─────────
    #
    # We take the highest-priority match per (company_id, user) —
    # if somehow multiple employees in the same company shared an email
    # we keep the lowest employees.id and leave the others NULL. The
    # UNIQUE(company_id, user_id) constraint forbids duplicates anyway.
    bind.execute(sa.text("""
        UPDATE employees
           SET user_id = m.user_id
          FROM (
              SELECT e.id AS emp_id,
                     u.id AS user_id
                FROM employees e
                JOIN users u
                  ON LOWER(u.email) = LOWER(e.email)
               WHERE e.email IS NOT NULL AND e.email <> ''
                 AND e.id = (
                     SELECT MIN(e2.id)
                       FROM employees e2
                      WHERE e2.company_id = e.company_id
                        AND LOWER(e2.email) = LOWER(u.email)
                 )
          ) AS m
         WHERE employees.id = m.emp_id
    """)) if bind.dialect.name != "sqlite" else _sqlite_backfill(bind)

    # ── Drop users.employee_id ─────────────────────────────────────
    #
    # batch_alter_table re-creates the table from the SQLAlchemy
    # metadata reflection; the FK on the dropped column disappears with
    # it. We don't call drop_constraint explicitly because the FK name
    # is dialect-generated on SQLite (would need to be looked up).
    if _has_col("users", "employee_id"):
        with op.batch_alter_table("users") as batch:
            batch.drop_column("employee_id")


def _sqlite_backfill(bind):
    """SQLite doesn't support UPDATE ... FROM the same way. Do it in
    Python with two round-trips."""
    rows = bind.execute(sa.text("""
        SELECT e.id AS emp_id, u.id AS user_id
          FROM employees e
          JOIN users u ON LOWER(u.email) = LOWER(e.email)
         WHERE e.email IS NOT NULL AND e.email <> ''
    """)).fetchall()

    # (company_id, user_id) → keep the smallest emp_id
    picked = {}
    # We need company_id too for uniqueness dedup.
    for r in bind.execute(sa.text("""
        SELECT e.id AS emp_id, e.company_id AS cid, u.id AS user_id
          FROM employees e
          JOIN users u ON LOWER(u.email) = LOWER(e.email)
         WHERE e.email IS NOT NULL AND e.email <> ''
      ORDER BY e.id
    """)).fetchall():
        key = (r.cid, r.user_id)
        if key not in picked:
            picked[key] = r.emp_id

    for (_cid, user_id), emp_id in picked.items():
        bind.execute(
            sa.text("UPDATE employees SET user_id = :uid WHERE id = :eid"),
            {"uid": user_id, "eid": emp_id},
        )


def downgrade():
    bind = op.get_bind()

    if not _has_col("users", "employee_id"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("employee_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_users_employee_id_employees",
                "employees", ["employee_id"], ["id"], ondelete="SET NULL",
            )

    # Back-copy: for each user with any employee, pick the lowest emp id.
    bind.execute(sa.text("""
        UPDATE users
           SET employee_id = (
               SELECT MIN(e.id)
                 FROM employees e
                WHERE e.user_id = users.id
           )
         WHERE EXISTS (SELECT 1 FROM employees e WHERE e.user_id = users.id)
    """))

    if _has_col("employees", "user_id"):
        try:
            op.drop_constraint(
                "uq_employees_company_user", "employees", type_="unique",
            )
        except Exception:
            pass
        try:
            op.drop_index("ix_employees_user_id", table_name="employees")
        except Exception:
            pass
        with op.batch_alter_table("employees") as batch:
            try:
                batch.drop_constraint(
                    "fk_employees_user_id_users", type_="foreignkey",
                )
            except Exception:
                pass
            batch.drop_column("user_id")
