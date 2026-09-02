"""MARSOUD-PRODUCT-BUNDLES-01 (2026-09-02) — bundle components.

A bundle = a `Product` with `is_bundle=True` whose `bundle_components`
enumerate the real ProductVariants that get deducted at POS sale
time. Deliberately separate from `manufacturing.BillOfMaterial` +
`WorkOrder`: this is a light-weight, sale-time expansion for cashier
sales; BOM/WorkOrder is the production cycle. The two coexist.

Design rules:
  * bundle_product must have `is_bundle=True` (enforced by the
    service that persists rows here; the DB column doesn't
    enforce it).
  * component_variant must belong to a product with `is_tracked=True`
    AND `is_bundle=False` — no bundle-in-bundle (§5 of the ticket).
  * qty_per_bundle > 0.
"""
from app import db


class BundleComponent(db.Model):
    __tablename__ = "bundle_components"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    bundle_product_id = db.Column(
        db.Integer, db.ForeignKey("products.id"),
        nullable=False, index=True)
    component_variant_id = db.Column(
        db.Integer, db.ForeignKey("product_variants.id"),
        nullable=False)
    qty_per_bundle = db.Column(db.Numeric(15, 3),
                                nullable=False, default=1)

    bundle_product = db.relationship(
        "Product", foreign_keys=[bundle_product_id],
        backref=db.backref("bundle_components",
                            cascade="all, delete-orphan"))
    component_variant = db.relationship(
        "ProductVariant", foreign_keys=[component_variant_id])

    __table_args__ = (
        db.UniqueConstraint("bundle_product_id", "component_variant_id",
                             name="uq_bundle_component"),
    )
