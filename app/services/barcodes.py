"""MARSOUD-ERP-01 Phase 3 — barcode generation.

Wraps python-barcode + PIL to render PNG images on-demand.
Variants without a supplier barcode get a deterministic auto-assigned
value derived from variant.id so the print sheet works for everyone.
"""
import io

from app import db
from app.models import ProductVariant


class BarcodeError(Exception):
    """Raised on unsupported format or rendering failure."""


SUPPORTED_FORMATS = ("CODE128",)


def auto_assigned_value(variant):
    """Deterministic placeholder barcode for variants without a supplier
    barcode. Format: PRD-NNNNNN (6-digit zero-padded variant id).
    """
    return f"PRD-{variant.id:06d}"


def assign_barcode_if_missing(variant):
    """If the variant has no barcode, set it to the auto-assigned value
    and commit. Returns the (possibly-newly-assigned) barcode string.
    """
    if not variant.barcode:
        variant.barcode = auto_assigned_value(variant)
        db.session.commit()
    return variant.barcode


def generate_barcode_png(value, *, format="CODE128",
                         module_height=12.0, font_size=10,
                         text_distance=4.0):
    """Render a Code128 (default) PNG into a BytesIO.

    `value` is the string to encode (the literal barcode). The PNG width
    is variable (depends on string length); height scales with
    module_height. Caller is responsible for catching BarcodeError.
    """
    fmt = (format or "CODE128").upper()
    if fmt not in SUPPORTED_FORMATS:
        raise BarcodeError(f"barcode format غير مدعوم: {fmt}")

    try:
        import barcode as _bc
        from barcode.writer import ImageWriter
    except ImportError as e:
        raise BarcodeError(f"مكتبة python-barcode غير مثبتة: {e}")

    options = {
        "module_height": module_height,
        "font_size": font_size,
        "text_distance": text_distance,
        "write_text": True,
    }
    klass = _bc.get_barcode_class("code128")
    obj = klass(str(value), writer=ImageWriter())
    buf = io.BytesIO()
    try:
        obj.write(buf, options=options)
    except Exception as e:
        raise BarcodeError(f"تعذّر رسم الباركود: {e}")
    buf.seek(0)
    return buf


def variants_for_print_sheet(company_id, variant_ids):
    """Resolve + auto-assign barcodes for a print run."""
    out = []
    if not variant_ids:
        return out
    rows = ProductVariant.query.filter(
        ProductVariant.company_id == company_id,
        ProductVariant.id.in_(variant_ids),
    ).all()
    for v in rows:
        if not v.barcode:
            v.barcode = auto_assigned_value(v)
    db.session.commit()
    return rows
