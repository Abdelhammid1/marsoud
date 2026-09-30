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
