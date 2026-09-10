"""MARSOUD-ACCOUNT-DELETION-01 (2026-09-11) — the "delete my account"
service backing the /account/delete route + the mobile app's
equivalent surface.

Apple App Store guideline 5.1.1(v) and Google Play's User Data
policy both require an in-app path to delete one's account. The
web has to satisfy the same bar so a user who signed up online can
close their file the same way they opened it.

Deletion semantics — three cases:

  1. User is a member of company C but NOT its sole owner
     → delete the user_companies row for (user, C) so C keeps
       running under the other owner(s). Personal user row is
       deleted at the end.
  2. User is the SOLE owner of company C, and C has other members
     → refuse; ask them to transfer ownership first (or hand-delete
       the company via a separate flow). This is safer than
       silently orphaning a business somebody else depends on.
  3. User is the SOLE owner of company C, and C has no other members
     → delete C in full (cascade wipes every child row: invoices,
       customers, vendor_bills, journal entries, etc. every
       tenant-scoped table has `company_id` with ondelete=CASCADE).

The final step is deleting the User row itself, which cascades
through any user-scoped rows (activity_log author refs, invitations
sent, etc.) via existing FK rules.

Everything runs in one transaction. If any step refuses, the whole
thing rolls back and the user stays exactly as they were.
"""
from datetime import datetime
import logging

from sqlalchemy import text as _sql_text

from app import db
from app.models import User, Company
from app.models.user import user_companies


logger = logging.getLogger("ledgeros.account_deletion")


class AccountDeletionError(Exception):
    """A user-visible refusal. Code identifies the reason so the
    route can render the right Arabic message + next steps."""
    def __init__(self, code, message, *, blockers=None):
        super().__init__(message)
        self.code = code
        self.blockers = blockers or []


def _user_memberships(user_id):
    """Return [(company, role), ...] for every company this user is
    a member of."""
    rows = db.session.execute(
        user_companies.select().where(
            user_companies.c.user_id == user_id)
    ).fetchall()
    result = []
    for row in rows:
        c = db.session.get(Company, row.company_id)
        if c is not None:
            result.append((c, row.role))
    return result


def _other_owners_count(company_id, exclude_user_id):
    """How many OTHER active users hold the 'owner' role on this
    company besides `exclude_user_id`."""
    n = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == company_id)
            & (user_companies.c.role == "owner")
            & (user_companies.c.user_id != exclude_user_id))
    ).rowcount
    # rowcount on SELECT is unreliable on some backends; count from
    # the iterable to be safe.
    rows = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == company_id)
            & (user_companies.c.role == "owner")
            & (user_companies.c.user_id != exclude_user_id))
    ).fetchall()
    return len(rows)


def _other_members_count(company_id, exclude_user_id):
    """How many OTHER users (any role) belong to this company besides
    `exclude_user_id`."""
    rows = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == company_id)
            & (user_companies.c.user_id != exclude_user_id))
    ).fetchall()
    return len(rows)


def preview_deletion(user):
    """Analyse the user's account without doing anything. Returns
    {
      'companies_to_delete':      [Company, …],   # sole owner + no others
      'memberships_to_remove':    [Company, …],   # membership only
      'blockers':                 [(Company, msg), …],  # sole owner + others
      'can_proceed':              bool,           # no blockers
    }
    The route uses this to render the warning banner + list of
    what will actually happen when the user confirms.
    """
    result = {
        "companies_to_delete": [],
        "memberships_to_remove": [],
        "blockers": [],
    }
    for company, role in _user_memberships(user.id):
        if role != "owner":
            result["memberships_to_remove"].append(company)
            continue
        # role == "owner"
        n_others = _other_members_count(company.id, user.id)
        if n_others == 0:
            result["companies_to_delete"].append(company)
        else:
            n_other_owners = _other_owners_count(company.id, user.id)
            if n_other_owners > 0:
                # There's another owner; membership just goes away.
                result["memberships_to_remove"].append(company)
            else:
                # Sole owner, but non-owner members exist. Refuse.
                result["blockers"].append((
                    company,
                    "أنت المالك الوحيد لهذه الشركة، وفيها موظفون / "
                    "محاسبون آخرون. حوّل الملكية لعضو آخر أو احذف "
                    "الشركة نفسها أولاً من إعدادات الشركة."
                ))
    result["can_proceed"] = not result["blockers"]
    return result


def delete_account(user, *, actor_id=None):
    """Execute the plan preview_deletion described.

    Raises AccountDeletionError with code='blocked' if there are
    unresolved sole-owner-with-others companies. The route catches
    and re-renders the page with the blockers listed.

    Non-idempotent by design: after this returns True the User row
    is gone and any subsequent call with a stale reference will
    error.  Callers must log the user out and drop any session
    state pointing at the row.
    """
    plan = preview_deletion(user)
    if not plan["can_proceed"]:
        raise AccountDeletionError(
            "blocked",
            "لا يمكن حذف الحساب — يوجد شركات لا يمكن حذفها تلقائياً",
            blockers=plan["blockers"])

    uid = user.id
    email = user.email
    n_companies_deleted = len(plan["companies_to_delete"])
    n_memberships_removed = len(plan["memberships_to_remove"])

    # 1. Delete every company where the user was the sole owner-and-only-
    # member. SQLAlchemy's session-level cascade doesn't know about
    # SQLite's ON DELETE CASCADE and tries to NULL out `company_id`
    # references before it deletes the parent — which fails on the
    # NOT NULL columns. Wipe children directly via `sorted_tables`
    # so we bypass the session cascade entirely.
    from sqlalchemy import inspect as _sql_inspect
    _insp = _sql_inspect(db.engine)
    for company in plan["companies_to_delete"]:
        cid_del = company.id
        # Belt: clear the membership rows for this company first so
        # the User row isn't the last handle on it.
        db.session.execute(user_companies.delete().where(
            user_companies.c.company_id == cid_del))
        # Now wipe every tenant-scoped child row, table by table,
        # deepest-first so FKs cascade cleanly.
        for t in reversed(db.metadata.sorted_tables):
            if t.name == "companies":
                continue
            try:
                cols = {c["name"] for c in _insp.get_columns(t.name)}
            except Exception:
                continue
            if "company_id" in cols:
                db.session.execute(_sql_text(
                    f"DELETE FROM {t.name} "
                    f"WHERE company_id = :c"), {"c": cid_del})
        # And the company row itself. session.delete would still trip
        # on stale in-memory relationship state; use raw SQL.
        db.session.execute(_sql_text(
            "DELETE FROM companies WHERE id = :c"),
            {"c": cid_del})
        db.session.expire_all()

    # 2. Remove the user's membership from companies they still leave
    # behind for other owners.
    for company in plan["memberships_to_remove"]:
        db.session.execute(user_companies.delete().where(
            (user_companies.c.user_id == uid)
            & (user_companies.c.company_id == company.id)))

    # 3. Delete tables that FK back to `users.id` with no cascade of
    # their own — otherwise the User delete would trip a FK
    # constraint. The safe list is: activity_log, invitations, and
    # any *_created_by / *_by columns.  Instead of chasing every
    # column, we NULL out FK refs whose columns are nullable; where
    # they're NOT nullable the row is deleted.  This is a
    # conservative sweep so a single missed FK doesn't crash the
    # whole flow.
    _blank_or_delete_user_refs(uid)

    # 4. The user row itself.
    db.session.delete(user)
    db.session.commit()

    logger.info(
        "Account deleted: user_id=%s email=%s "
        "companies_deleted=%s memberships_removed=%s "
        "requested_by=%s",
        uid, email, n_companies_deleted, n_memberships_removed,
        actor_id or uid)

    return {
        "user_id": uid,
        "email": email,
        "companies_deleted": n_companies_deleted,
        "memberships_removed": n_memberships_removed,
    }


def _blank_or_delete_user_refs(user_id):
    """Handle user-FK-referencing rows that don't have their own
    cascade. For every table with a `<name>_id INTEGER REFERENCES
    users(id)` column, we NULL the ref if the column is nullable,
    or delete the row otherwise.

    Doing this via inspect() rather than a hardcoded table list
    keeps the sweep robust against new tables added later.
    """
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    tables = insp.get_table_names()
    for tbl in tables:
        if tbl == "users":
            continue
        try:
            fks = insp.get_foreign_keys(tbl)
        except Exception:
            continue
        for fk in fks:
            if fk.get("referred_table") != "users":
                continue
            # Which local column FKs to users.id?
            if not fk.get("constrained_columns"):
                continue
            col = fk["constrained_columns"][0]
            cols_info = {c["name"]: c for c in insp.get_columns(tbl)}
            col_info = cols_info.get(col, {})
            is_nullable = col_info.get("nullable", True)
            if is_nullable:
                db.session.execute(_sql_text(
                    f"UPDATE {tbl} SET {col} = NULL "
                    f"WHERE {col} = :uid"), {"uid": user_id})
            else:
                db.session.execute(_sql_text(
                    f"DELETE FROM {tbl} WHERE {col} = :uid"),
                    {"uid": user_id})
