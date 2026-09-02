"""MARSOUD-PRODUCT-BUNDLES-01 (2026-09-02) — bundle expansion + display.

Isolated bundle math so POS stays thin and the allocator is
audit-testable on its own.

Contract:
  * `expand_bundle_line` takes a single bundle sale (product + qty +
    price) and returns N per-component line dicts. Σ(line_total)
    exactly equals qty × unit_price to the cent (rounding remainder
    absorbed by the last row).
  * `expand_bundle_items` walks the POS `items` list and inflates
    any bundle line in place; non-bundle lines pass through.
  * `check_bundle_availability` is the pre-flight the POS `/lookup`
    endpoint runs so the cashier sees "not enough stock" BEFORE
    the customer pays.
  * `validate_bundle_components` runs when the operator saves a
    bundle definition — refuses empty component lists, bundle-in-
    bundle, and non-tracked (service) components.
  * `group_items_for_display` splits an invoice's items into
    (standalone, bundle_groups) for receipts + PDF invoices to
    render "one bundle line + collapsed components".

Never called from `record_sale`, `post_invoice_to_ledger`,
`inventory.py`, or `ledger.py`. The bundle boundary ends at
`create_pos_order`.
"""
import uuid
from app import db
from app.models import (
    Product, ProductVariant, BundleComponent, InvoiceItem,
)


class BundleError(Exception):
    """Raised on bundle validation failure. Callers can treat as a
    plain domain error; POS wraps into POSError so the existing
    catch-and-flash chain works unchanged."""


# ─── Definition-time validation ─────────────────────────────
def validate_bundle_components(bundle_product, components_data):
    """Raise BundleError if the component set is invalid.

    `components_data` is a list of dicts each with
    `variant_id, qty_per_bundle`. Empty list, bundle-in-bundle, and
    non-tracked (service) components are all refused.
    """
    if not components_data:
        raise BundleError(
            f"الباقة '{bundle_product.name}' من غير مكونات")
    for row in components_data:
        vid = int(row.get("variant_id") or 0)
        qty = float(row.get("qty_per_bundle") or 0)
        if qty <= 0:
            raise BundleError(
                "كمية كل مكوّن في الباقة يجب أن تكون أكبر من صفر")
        v = db.session.get(ProductVariant, vid)
        if not v or v.company_id != bundle_product.company_id:
            raise BundleError(f"المكوّن #{vid} غير موجود")
        p = v.product
        if not p:
            raise BundleError(f"المكوّن #{vid} بدون منتج مرتبط")
        if getattr(p, "is_bundle", False):
            raise BundleError(
                f"لا يمكن استخدام باقة داخل باقة "
                f"({p.name} باقة بحد ذاتها)")
        if not getattr(p, "is_tracked", True):
            raise BundleError(
                f"المكوّن '{p.name}' خدمة/غير متتبَّع — "
                "الباقة تحتاج مكونات لها مخزون فعلي")
    return True


# ─── Sale-time expansion ────────────────────────────────────
def expand_bundle_line(bundle_product, bundle_qty, bundle_unit_price):
    """Return a list of dicts ready to become InvoiceItems.

    Each dict: `variant_id, qty, unit_price`. Allocation weight per
    §5: `weight_i = component.default_price × qty_per_bundle`. All
    zero weights → split evenly. Last row absorbs the rounding
    remainder so `sum(line_total) == bundle_qty × bundle_unit_price`
    to the cent.
    """
    components = list(bundle_product.bundle_components or [])
    if not components:
        raise BundleError(
            f"الباقة '{bundle_product.name}' من غير مكونات — "
            "أضف مكونات قبل البيع")

    weights = []
    for c in components:
        p = c.component_variant.product if c.component_variant else None
        base_price = float((p.default_price if p else 0) or 0)
        weights.append(base_price * float(c.qty_per_bundle or 0))
    total_weight = sum(weights)
    if total_weight <= 0:
        weights = [1.0] * len(components)
        total_weight = float(len(components))

    total_bundle_value = round(float(bundle_unit_price)
                                * float(bundle_qty), 2)
    lines = []
    running_total = 0.0
    for i, c in enumerate(components):
        qty = float(c.qty_per_bundle) * float(bundle_qty)
        if i < len(components) - 1:
            line_value = round(
                total_bundle_value * weights[i] / total_weight, 2)
            running_total = round(running_total + line_value, 2)
        else:
            # Last row absorbs the rounding remainder so the sale
            # total matches the bundle price to the cent.
            line_value = round(total_bundle_value - running_total, 2)
        unit_price = (line_value / qty) if qty > 0 else 0
        lines.append({
            "variant_id": c.component_variant_id,
            "qty": qty,
            "unit_price": unit_price,
            "line_value_hint": line_value,
        })
    return lines


def expand_bundle_items(company_id, items, warehouse=None):
    """Rewrite `items` in place — any entry pointing at a bundle
    variant becomes N per-component entries with a shared bundle_ref.

    Non-bundle lines pass through untouched. Every produced dict
    carries the fields `create_pos_order` reads (variant_id, qty,
    unit_price, discount_type, discount_value, unit_id, sold_pieces)
    plus the two new bundle markers.
    """
    out = []
    for line in items:
        try:
            variant_id = int(line["variant_id"])
        except (KeyError, ValueError, TypeError):
            out.append(line)
            continue
        variant = db.session.get(ProductVariant, variant_id)
        if not variant or variant.company_id != company_id:
            # Let the downstream POS guard handle the "invalid variant"
            # message with its existing wording.
            out.append(line)
            continue
        prod = variant.product
        if not (prod and getattr(prod, "is_bundle", False)):
            out.append(line)
            continue

        qty = float(line.get("qty") or 0)
        unit_price = float(line.get("unit_price") or 0)
        # Optional pre-flight — kept independent of the /lookup call
        # so a hand-crafted POST can't skip the guard.
        if warehouse is not None:
            ok, msg = check_bundle_availability(prod, qty, warehouse)
            if not ok:
                raise BundleError(msg or "مكوّن غير كافٍ")
        expanded = expand_bundle_line(prod, qty, unit_price)
        bundle_ref = uuid.uuid4().hex[:12]
        for e in expanded:
            out.append({
                "variant_id": e["variant_id"],
                "qty": e["qty"],
                "unit_price": e["unit_price"],
                # Carry POS-line meta as-is (discount / unit / pieces).
                "discount_type": line.get("discount_type", "NONE"),
                "discount_value": 0,   # discount already priced into
                                        # the bundle headline
                "unit_id": None,       # components fall back to base unit
                "sold_pieces": None,
                # Bundle markers
                "bundle_ref": bundle_ref,
                "bundle_product_id": prod.id,
            })
    return out


# ─── Pre-flight availability ────────────────────────────────
def check_bundle_availability(bundle_product, bundle_qty, warehouse):
    """(True, None) or (False, arabic_message).

    Called from /pos/lookup so the cashier sees the warning BEFORE
    the customer taps pay. Also called from `expand_bundle_items` as
    a belt-and-suspenders guard when a warehouse is passed in.
    """
    if bundle_qty <= 0:
        return False, "الكمية يجب أن تكون أكبر من صفر"
    for c in bundle_product.bundle_components or []:
        needed = float(c.qty_per_bundle or 0) * float(bundle_qty)
        v = c.component_variant
        if not v:
            return False, "أحد مكونات الباقة غير موجود"
        try:
            available = float(v.balance_in(warehouse).qty or 0)
        except Exception:
            available = 0
        if available + 0.0001 < needed:
            return False, (
                f"الكمية غير كافية من '{v.display_name}' "
                f"(متاح {available:g}, مطلوب {needed:g}) "
                f"لتركيب باقة '{bundle_product.name}'"
            )
    return True, None


# ─── Receipt / PDF grouping ─────────────────────────────────
def group_items_for_display(invoice_items):
    """Return `(standalone, bundle_groups)`.

    `standalone` = ordered list of InvoiceItem rows that did NOT
    come from a bundle.
    `bundle_groups` = list of dicts:
        {bundle_product, bundle_product_name, components, total, qty}
    grouped by bundle_ref so the receipt shows "طقم إفطار × 2 —
    90.00" once with a small "شامل: شاي، سكر، بسكويت" underneath.

    A bundle's headline qty is derived from any single component:
    `component.quantity / component.qty_per_bundle` — the allocator
    kept this ratio exact. Falls back to 1 if the math is fishy.
    """
    groups = {}
    standalone = []
    for item in invoice_items:
        if item.bundle_ref:
            g = groups.setdefault(item.bundle_ref, {
                "bundle_ref": item.bundle_ref,
                "bundle_product_id": item.bundle_product_id,
                "bundle_product": None,
                "bundle_product_name": None,
                "components": [],
                "total": 0.0,
                "qty": None,
            })
            g["components"].append(item)
            g["total"] += float(item.line_total or 0)
        else:
            standalone.append(item)

    # Second pass: fill in name + qty. Prefer the model, fall back to
    # sensible strings so a mid-migration reader still gets something.
    for g in groups.values():
        if g["bundle_product_id"]:
            p = db.session.get(Product, g["bundle_product_id"])
            g["bundle_product"] = p
            g["bundle_product_name"] = p.name if p else "باقة"
        else:
            g["bundle_product_name"] = "باقة"
        # Derive headline qty from the first component with a known
        # qty_per_bundle (BundleComponent row). Best-effort — if the
        # bundle definition changed between sale and render, we still
        # print the components correctly.
        if g["components"]:
            first = g["components"][0]
            bc = None
            if g["bundle_product"] and first.variant_id:
                for c in (g["bundle_product"].bundle_components or []):
                    if c.component_variant_id == first.variant_id:
                        bc = c; break
            if bc and float(bc.qty_per_bundle or 0) > 0:
                g["qty"] = float(first.quantity or 0) / float(bc.qty_per_bundle)
            else:
                g["qty"] = 1
    return standalone, list(groups.values())
