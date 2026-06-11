from datetime import datetime
from app import db


class Product(db.Model):
    """Saved service or product line, used to pre-fill invoice items without
    locking the user out of free-form entry.
    """
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    default_price = db.Column(db.Numeric(15, 4), default=0)
    default_tax_rate = db.Column(db.Numeric(5, 2))
    sku = db.Column(db.String(50))
    is_active = db.Column(db.Boolean, default=True)
    # ERP-01 — distinguishes a tracked good (variants own stock, COGS posts
    # on sale) from a service (no stock movement, no COGS).
    is_tracked = db.Column(db.Boolean, default=True, nullable=False)
    default_unit = db.Column(db.String(20), default="قطعة", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    company = db.relationship("Company", backref=db.backref("products", lazy="dynamic"))

    @property
    def default_variant(self):
        """First active variant — every product is migrated with one.

        Defined as a property (not a relationship) so callers don't have
        to remember to filter by is_active.
        """
        from app.models.inventory import ProductVariant
        return ProductVariant.query.filter_by(
            product_id=self.id, is_active=True,
        ).order_by(ProductVariant.id.asc()).first()
