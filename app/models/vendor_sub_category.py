"""MARSOUD-VENDOR-SUBCAT (Abdelhamid 2026-07-14) — per-vendor sub-category.

Some vendors bundle multiple products/services under one bill (Claude
→ Abdelhamid's subscription, Rofida's subscription, API credits,
Team plan …). The single free-text description field on
VendorBillItem was the only way to distinguish them, which made
category-level spending reports unreliable.

This model adds a fixed-value taxonomy owned by each vendor:
  · one row per vendor + name (unique).
  · is_active flag to hide from pickers without breaking historical
    bill lines that reference it.
  · Sub-categories are never accounting entities — no journal impact
    of any kind. They're a tagging layer for reports only.

Delete rules enforced in the service layer:
  · A sub-category referenced by a VendorBillItem cannot be deleted.
  · Deactivation is always safe (just hides from the picker).
"""
from datetime import datetime

from app import db


class VendorSubCategory(db.Model):
    __tablename__ = "vendor_sub_categories"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    # Owned by exactly ONE vendor — no shared taxonomies. Ticket
    # spec: "Supplier ⬇️ Sub Categories".
    vendor_id = db.Column(db.Integer,
                           db.ForeignKey("vendors.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True,
                           index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                               nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    company = db.relationship("Company")
    vendor = db.relationship("Vendor")
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        # Same name is reused across vendors freely (Claude/Rofida
        # and Google/Rofida can both exist), but must be unique
        # within (company, vendor).
        db.UniqueConstraint("company_id", "vendor_id", "name",
                             name="uq_vendor_subcat_name"),
    )

    def __repr__(self):
        return (f"<VendorSubCategory {self.id} "
                f"'{self.name}' vendor={self.vendor_id}>")
