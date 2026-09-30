"""Account-tree rule: any account that has children is a grouping header."""
from app import db


class AccountTreeError(Exception):
    pass


def make_parent_group(parent):
    """Flip `parent` to a header (is_postable=False) once it gets a child.

    Refuses when the parent still carries direct journal lines, because a
    header's balance is the roll-up of its children only.
    """
    if parent is None or not parent.is_postable:
        return
    from app.models.journal import JournalLine
    has_lines = db.session.query(JournalLine).filter(
        JournalLine.account_id == parent.id).first()
    if has_lines:
        raise AccountTreeError(
            "لا يمكن الإضافة "
            "تحت حساب عليه "
            "قيود مباشرة، "
            "انقلها أولاً")
    parent.is_postable = False


def restore_leaf_if_childless(parent):
    """Undo make_parent_group when the last child leaves.

    Only for accounts that are NOT seed headers (1120, 1130... stay
    headers forever). Call after the child is deleted/moved + flushed.
    """
    if parent is None or parent.is_postable:
        return
    from app.models import Account
    if db.session.query(Account.id).filter(
            Account.parent_id == parent.id).first():
        return
    from app.services.seed_coa import DEFAULT_COA
    seed_headers = {r[0] for r in DEFAULT_COA if len(r) >= 6 and not r[5]}
    if parent.code in seed_headers:
        return
    parent.is_postable = True
