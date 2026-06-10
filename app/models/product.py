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
    created_at = db.Column(db.DateTime, default=datetime.now)

    company = db.relationship("Company", backref=db.backref("products", lazy="dynamic"))
