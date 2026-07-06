"""MARSOUD-UNIT-CONVERSION-01 — product-unit conversion helpers.

Design:
  - Every tracked Product has one is_base=True ProductUnit (factor=1).
  - Any additional unit is either bigger (e.g. كرتونة factor=30) or
    smaller (e.g. نصف حبة factor=0.5) than the base.
  - convert_to_base() is called at posting time to translate the
    cashier's (quantity, unit_id) input into the base_quantity that
    the inventory engine actually consumes.

Backward compatibility:
  - unit_id=None means the input is ALREADY in the base unit —
    base_quantity = quantity. Old rows written before this ticket land
    here automatically; the migration also plants a base unit on every
    tracked product so the "unit_id=None" path is only hit for future
    ad-hoc calls (POS pre-populated with the base unit still passes
    unit_id, so this fallback is defensive not typical).
"""
from decimal import Decimal
from app import db
from app.models import ProductUnit, Product


class UnitError(ValueError):
    """Domain-specific error so route code can catch it separately."""
    pass


# ─── Read-side ──────────────────────────────────────────────────────────
def ensure_base_unit(product):
    """Idempotent: create a base unit for `product` if none exists.

    Used by the products/new route so a freshly-created product is
    immediately usable in POS/invoices without a manual "define units"
    step. Returns the ProductUnit row (existing or freshly created)."""
    existing = ProductUnit.query.filter_by(
        product_id=product.id, is_base=True,
    ).first()
    if existing:
        return existing
    default_name = (product.default_unit or "قطعة").strip() or "قطعة"
    row = ProductUnit(
        company_id=product.company_id, product_id=product.id,
        unit_name=default_name, conversion_factor=Decimal("1"),
        is_base=True,
    )
    db.session.add(row); db.session.flush()
    return row


def convert_to_base(product, qty, unit_id=None):
    """Return the base-unit equivalent of `qty` when it's expressed in
    `unit_id`. Never touches the DB; safe to call inside a bigger
    transaction.

    Rules:
      - unit_id=None → treat qty as already-in-base (returns qty).
      - unit_id set but belongs to a different product → raise UnitError.
      - qty < 0 → raise (matches the inventory engine's expectations;
        refunds pass positive qty and flip the sign at posting time).
    """
    qty_dec = Decimal(str(qty))
    if qty_dec < 0:
        raise UnitError("الكمية يجب أن تكون رقم موجب")
    if unit_id in (None, 0, ""):
        return qty_dec
    u = db.session.get(ProductUnit, int(unit_id))
    if not u:
        raise UnitError(f"وحدة #{unit_id} غير موجودة")
    if product and u.product_id != product.id:
        raise UnitError(
            f"وحدة #{unit_id} لا تخص المنتج #{product.id}"
        )
    factor = Decimal(str(u.conversion_factor or 1))
    return qty_dec * factor


# ─── Write-side (used by /products/<id>/units) ─────────────────────────
def _parse_optional_price(raw):
    """Return Decimal(raw) or None. Raises UnitError if raw is
    provided but not a non-negative number."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        val = Decimal(s)
    except Exception:
        raise UnitError("سعر البيع غير صحيح")
    if val < 0:
        raise UnitError("سعر البيع يجب أن يكون رقم غير سالب")
    return val


def create_unit(product, unit_name, conversion_factor, sale_price=None):
    """Add a non-base unit to `product`."""
    name = (unit_name or "").strip()
    if not name:
        raise UnitError("اسم الوحدة مطلوب")
    try:
        factor = Decimal(str(conversion_factor))
    except Exception:
        raise UnitError("معامل التحويل غير صحيح")
    if factor <= 0:
        raise UnitError("معامل التحويل يجب أن يكون أكبر من صفر")
    # Refuse if a unit with the same name already exists on this product.
    dup = ProductUnit.query.filter_by(
        product_id=product.id, unit_name=name,
    ).first()
    if dup:
        raise UnitError(f"وحدة بنفس الاسم ({name}) موجودة على المنتج")
    row = ProductUnit(
        company_id=product.company_id, product_id=product.id,
        unit_name=name, conversion_factor=factor, is_base=False,
        sale_price=_parse_optional_price(sale_price),
    )
    db.session.add(row); db.session.flush()
    return row


def set_unit_sale_price(unit, raw_price):
    """MARSOUD-UOM-PRICE — inline setter used by the units-management
    page. Passing an empty string clears the override (falls back to
    default_price × factor)."""
    unit.sale_price = _parse_optional_price(raw_price)
    db.session.flush()
    return unit


def _unit_has_movements(unit):
    """True if any historical InvoiceItem or VendorBillItem points at
    this unit. The check is used by both delete_unit() and (future)
    edit_unit() to refuse mutations that would corrupt already-frozen
    base_quantity snapshots."""
    from app.models import InvoiceItem
    from app.models.vendor_bill import VendorBillItem
    if InvoiceItem.query.filter_by(unit_id=unit.id).first():
        return True
    if VendorBillItem.query.filter_by(unit_id=unit.id).first():
        return True
    return False


def delete_unit(unit):
    """Refuses if the unit is base OR has been used in any posting."""
    if unit.is_base:
        raise UnitError("لا يمكن حذف وحدة الأساس")
    if _unit_has_movements(unit):
        raise UnitError(
            "لا يمكن حذف وحدة عليها حركات مخزون سابقة — "
            "غيّر معامل التحويل بدل الحذف."
        )
    db.session.delete(unit)
    db.session.flush()


def can_edit_factor(unit):
    """True if we can safely mutate this unit's conversion_factor.

    A unit that's been used in any transaction has already had its
    base_quantity frozen against the OLD factor; changing the factor
    now would silently break the retroactive math. So we only allow
    edits when no history exists."""
    return not _unit_has_movements(unit)
