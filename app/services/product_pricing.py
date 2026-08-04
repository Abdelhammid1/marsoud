"""MARSOUD-PACK-ONLY-PRICING — box in, per-piece out.

The product form used to ask for a per-piece price and a per-piece cost
while the user buys and sells by the box. Someone typed the box price
into the piece field and a piece sold for 2100 instead of 0.42. The
screen allowed the mistake, so the fields are gone: the user enters box
numbers, this module divides.

Everything funnels through `apply_pack_pricing()` so there is exactly
one place that can set a per-piece figure, and no route ever reads a
per-piece value from a form.

  pack_pieces = 1  → sold individually. The box price IS the piece
                     price and no pack unit is created (it would just
                     duplicate the base unit).
  service          → no purchase side at all; only a sale price.
"""
from app.services.units import (
    ensure_base_unit, create_unit, set_unit_sale_price, UnitError,
)


class PricingError(ValueError):
    """User-facing validation error on the pricing block."""


def _num(raw, label):
    if raw is None or str(raw).strip() == "":
        return 0.0
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        raise PricingError(f"{label} غير صالح")


def parse_pack_input(form, *, is_goods):
    """Pull the three box fields off a request form and validate them.

    Returns (pieces, pack_purchase, pack_sale, pack_name).
    """
    pack_name = (form.get("pack_unit_name") or "").strip() or "كرتونة"
    pack_sale = _num(form.get("pack_sale_price"), "سعر بيع العلبة")

    if not is_goods:
        # A service has no box and nothing to buy — one price, and the
        # rest of the block is hidden on the form.
        if pack_sale <= 0:
            raise PricingError("سعر البيع مطلوب ويجب أن يكون أكبر من صفر")
        return 1, 0.0, pack_sale, pack_name

    raw_pieces = (form.get("pieces_per_pack") or "").strip()
    try:
        pieces = int(float(raw_pieces)) if raw_pieces else 1
    except (TypeError, ValueError):
        raise PricingError("عدد القطع في العلبة غير صالح")
    if pieces < 1:
        raise PricingError(
            "عدد القطع في العلبة يجب أن يكون 1 على الأقل "
            "(لو المنتج بيتباع قطعة قطعة سيبها 1)")

    pack_purchase = _num(form.get("pack_purchase_price"), "سعر شراء العلبة")
    if pack_purchase <= 0:
        raise PricingError("سعر شراء العلبة مطلوب ويجب أن يكون أكبر من صفر")
    if pack_sale <= 0:
        raise PricingError("سعر بيع العلبة مطلوب ويجب أن يكون أكبر من صفر")

    return pieces, pack_purchase, pack_sale, pack_name


def apply_pack_pricing(product, variant, *, pieces, pack_purchase,
                       pack_sale, pack_name, is_goods):
    """Store the box numbers and derive every per-piece figure.

    Writes product.pack_pieces / pack_purchase_price / default_price,
    variant.unit_cost, and creates-or-updates the pack ProductUnit.
    Flushes but does not commit — the caller owns the transaction.

    Returns (unit_cost, unit_price) for display/opening-balance use.
    """
    pieces = max(1, int(pieces or 1))
    unit_cost = round(float(pack_purchase or 0) / pieces, 4)
    unit_price = round(float(pack_sale or 0) / pieces, 4)

    product.pack_pieces = pieces
    product.pack_purchase_price = pack_purchase or None
    product.default_price = unit_price
    if variant is not None:
        variant.unit_cost = unit_cost

    # Services carry no units at all — nothing to receive or convert.
    if not is_goods:
        return unit_cost, unit_price

    base = ensure_base_unit(product)

    if pieces <= 1:
        # Sold individually: the base unit already says everything.
        return unit_cost, unit_price

    if pack_name == base.unit_name:
        raise PricingError(
            "اسم العلبة مطابق لوحدة الأساس — "
            f"اختر اسم مختلف عن '{base.unit_name}'.")

    # Reuse the existing pack row when editing so a product doesn't grow
    # a new unit every time its pricing is touched. Match on the name we
    # manage, falling back to the row whose factor is the old box size.
    existing = None
    for u in product.units:
        if u.is_base:
            continue
        if u.unit_name == pack_name:
            existing = u
            break
    if existing is None:
        for u in product.units:
            if (not u.is_base
                    and float(u.conversion_factor or 0) == float(pieces)):
                existing = u
                break

    try:
        if existing is not None:
            existing.conversion_factor = pieces
            set_unit_sale_price(existing, pack_sale)
        else:
            create_unit(product, unit_name=pack_name,
                        conversion_factor=pieces, sale_price=pack_sale)
    except UnitError as e:
        raise PricingError(str(e))

    return unit_cost, unit_price


def pack_values_for(product):
    """The box numbers to prefill an edit form with.

    Reads back what was stored; falls back to the per-piece values for
    products created before this ticket (they simply look like
    individually-sold items, which is what they effectively were).
    """
    pieces = int(product.pack_pieces or 1) or 1
    purchase = product.pack_purchase_price
    if purchase is None:
        # Legacy row: reconstruct from the default variant's cost so the
        # form isn't blank. Exact when pieces == 1, which is the case
        # for everything created before this ticket.
        v = product.default_variant
        purchase = float(v.unit_cost or 0) * pieces if v else 0.0
    sale = None
    if pieces > 1:
        for u in product.units:
            if (not u.is_base
                    and float(u.conversion_factor or 0) == float(pieces)
                    and u.sale_price is not None):
                sale = float(u.sale_price)
                break
    if sale is None:
        sale = float(product.default_price or 0) * pieces
    pack_name = "كرتونة"
    for u in product.units:
        if not u.is_base and float(u.conversion_factor or 0) == float(pieces):
            pack_name = u.unit_name
            break
    return {
        "pieces": pieces,
        "pack_purchase_price": round(float(purchase or 0), 4),
        "pack_sale_price": round(float(sale or 0), 4),
        "pack_unit_name": pack_name,
    }
