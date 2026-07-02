"""MARSOUD-PARTY-OPENING-BALANCE-01 — audit-only row for the one-shot
opening balance we record when a customer or vendor is first coded.

Kept separate from Customer/Vendor so the trigger (idempotence check,
who entered it, when) is easy to inspect without joining. Every row
also carries the journal_entry_id it produced so a downstream reader
can hop straight to the ledger."""
import enum
from datetime import datetime, date
from app import db


class PartyType(str, enum.Enum):
    CUSTOMER = "CUSTOMER"
    VENDOR = "VENDOR"


class PartyOpeningBalance(db.Model):
    __tablename__ = "party_opening_balances"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                             nullable=False, index=True)
    party_type = db.Column(db.Enum(PartyType), nullable=False, index=True)
    party_id = db.Column(db.Integer, nullable=False, index=True)
    # Signed amount. Positive = party owes us (customer default) OR we
    # owe party (vendor default); the service picks the correct dr/cr
    # from party_type. Negative is allowed (customer with advance paid,
    # vendor we already prepaid).
    amount = db.Column(db.Numeric(15, 4), nullable=False)
    entry_date = db.Column(db.Date, nullable=False, default=date.today)
    journal_entry_id = db.Column(db.Integer,
                                    db.ForeignKey("journal_entries.id"),
                                    nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.now)

    __table_args__ = (
        db.UniqueConstraint(
            "company_id", "party_type", "party_id",
            name="uq_party_opening_balance",
        ),
    )
