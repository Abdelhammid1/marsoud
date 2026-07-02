from datetime import datetime
from app import db


class ProductGroup(db.Model):
    """MARSOUD-PRODUCT-HIERARCHY-01 — top level of the product tree.

    Every category belongs to a group; every product belongs to a
    category. Groups are per-company. Modeled loosely on Account's
    header-vs-leaf split, but 2 concrete levels (not a recursive tree)
    because the ticket calls out "3 مستويات، مفيش أكتر".
    """
    __tablename__ = "product_groups"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                             nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company")
    categories = db.relationship(
        "ProductCategory", back_populates="group",
        order_by="ProductCategory.name.asc()",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        db.UniqueConstraint("company_id", "name",
                              name="uq_product_group_company_name"),
    )


class ProductCategory(db.Model):
    """MARSOUD-PRODUCT-HIERARCHY-01 — middle level of the product tree."""
    __tablename__ = "product_categories"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                             nullable=False, index=True)
    group_id = db.Column(db.Integer,
                            db.ForeignKey("product_groups.id"),
                            nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company")
    group = db.relationship("ProductGroup", back_populates="categories")
    products = db.relationship("Product", back_populates="category")

    __table_args__ = (
        db.UniqueConstraint("company_id", "group_id", "name",
                              name="uq_product_category_group_name"),
    )


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
    # on sale) from a service (no stock movement, no COGS). Columns exist in
    # DB from migration m1a4e7c9b3f6.
    is_tracked = db.Column(db.Boolean, default=True, nullable=False)
    default_unit = db.Column(db.String(20), default="قطعة", nullable=False)
    # MARSOUD-PRODUCT-HIERARCHY-01 — required for new products; nullable
    # in the DB column for a smooth backfill (the migration fills every
    # existing row with a per-company default "عام" category). App-level
    # validation refuses new inserts without a category_id.
    category_id = db.Column(db.Integer,
                              db.ForeignKey("product_categories.id"),
                              nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    company = db.relationship("Company", backref=db.backref("products", lazy="dynamic"))
    category = db.relationship("ProductCategory", back_populates="products")

    @property
    def group(self):
        return self.category.group if self.category else None

    @property
    def base_unit(self):
        """MARSOUD-UNIT-CONVERSION-01 — the single is_base=True row for
        this product. Falls back to None if the migration hasn't been
        run yet (older data). Callers should treat None as "quantity is
        already the base unit"."""
        for u in self.units:
            if u.is_base:
                return u
        return None


class ProductUnit(db.Model):
    """MARSOUD-UNIT-CONVERSION-01 — sellable/purchasable unit with a
    conversion factor to the product's base unit.

    Every tracked product has exactly ONE row with is_base=True and
    conversion_factor=1. Any additional row represents a bigger or
    smaller unit — e.g. base='حبة', extra='كرتونة' factor=30 (one
    كرتونة = 30 حبة).

    The inventory engine (record_sale, receive_stock, weighted-average
    cost) still operates in base units only — the wiring layer at
    posting time is responsible for calling convert_to_base() before
    it touches the engine.
    """
    __tablename__ = "product_units"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                             nullable=False, index=True)
    product_id = db.Column(db.Integer,
                              db.ForeignKey("products.id", ondelete="CASCADE"),
                              nullable=False, index=True)
    unit_name = db.Column(db.String(50), nullable=False)
    conversion_factor = db.Column(db.Numeric(15, 6),
                                     nullable=False, default=1)
    is_base = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship(
        "Product",
        backref=db.backref("units", cascade="all, delete-orphan",
                             order_by="ProductUnit.is_base.desc(), "
                                        "ProductUnit.conversion_factor.asc()"),
    )
    company = db.relationship("Company")

    __table_args__ = (
        db.UniqueConstraint("product_id", "unit_name",
                              name="uq_product_unit_name"),
    )

    @property
    def display_label(self):
        """'كرتونة (30 حبة)' style — collapses to just 'حبة' for base."""
        if self.is_base:
            return self.unit_name
        base = self.product.base_unit
        base_name = base.unit_name if base else "أساسية"
        # Trim trailing zeros on the factor for a clean label.
        f = f"{float(self.conversion_factor):g}"
        return f"{self.unit_name} ({f} {base_name})"

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
