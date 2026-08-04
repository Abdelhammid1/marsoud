"""MARSOUD-CATEGORY-VISIBILITY-01 — which modules a category shows up in.

Raw materials are bought and consumed, never sold, but they appeared on
the POS cashier screen because product visibility was all-or-nothing. Each
ProductCategory now carries four independent switches, and every module
that lists products for an operator reads them THROUGH THIS MODULE.

That indirection is the point. The ticket's own warning was that each
module has more than one way in — a screen and a search — and the POS
proved it: the grid and the barcode scanner reached products by different
routes, so filtering the grid alone would still have let a hidden product
be scanned into the cart. One helper, used by every entry point, is what
keeps those in step.

ADDING A MODULE
===============
Add a row to MODULES and a matching column on ProductCategory. Nothing
else here changes.

DELIBERATELY NOT GATED
======================
  · the products catalog (routes/products.py) — an admin must always see
    every product they own; the ticket says so explicitly
  · inventory: counts, adjustments, transfers, barcode printing, reports
    — raw materials have real stock and must stay visible there
  · POST handlers — the ticket puts server-side enforcement against a
    forged product id out of scope. This hides things from pickers; it is
    not an authorisation boundary, and no caller should treat it as one.
"""
from sqlalchemy import or_

from app import db
from app.models import Product, ProductCategory


# module key → (ProductCategory column, Arabic label for the UI)
MODULES = {
    "pos": ("visible_in_pos", "نقطة البيع"),
    "manufacturing": ("visible_in_manufacturing", "التصنيع"),
    "vendor_bills": ("visible_in_vendor_bills", "فواتير الموردين"),
    "customer_invoices": ("visible_in_customer_invoices", "فواتير العملاء"),
}

# Rendering order + icon for the category screen, kept next to MODULES so
# a new module shows up in the UI without hunting through templates.
MODULE_ICONS = {
    "pos": "🛒",
    "manufacturing": "🏭",
    "vendor_bills": "📥",
    "customer_invoices": "🧾",
}


def _column(module):
    try:
        col_name, _label = MODULES[module]
    except KeyError:
        raise ValueError(
            f"unknown module {module!r} — expected one of "
            f"{sorted(MODULES)}")
    return getattr(ProductCategory, col_name)


def hidden_category_ids(company_id, module):
    """Ids of this company's categories switched OFF for `module`.

    Returns the HIDDEN set rather than the visible one on purpose. Every
    company starts with all four flags on, so this is empty, and callers
    can then skip filtering entirely — the guarded query stays byte-for-
    byte what it was before this feature existed. An allow-list would
    have made the default case a large `IN (...)` and any bug in it would
    silently hide real products.

    Recomputed on every call. There are a handful of categories per
    company, and the ticket requires a ticked box to take effect
    immediately, so nothing is cached.
    """
    col = _column(module)
    rows = db.session.query(ProductCategory.id).filter(
        ProductCategory.company_id == company_id,
        col.is_(False),
    ).all()
    return {r[0] for r in rows}


def product_visible_clause(company_id, module):
    """A filter criterion on Product, or None when nothing is hidden.

    Callers must handle None by not filtering at all:

        clause = product_visible_clause(cid, "pos")
        if clause is not None:
            q = q.filter(clause)

    Products with no category stay visible. `category_id` is nullable
    (products predating the hierarchy), and NULL NOT IN (...) evaluates to
    NULL, which SQL treats as false — so without the explicit IS NULL arm
    every uncategorised product would silently vanish from all four
    modules the first time anyone hid anything.
    """
    hidden = hidden_category_ids(company_id, module)
    if not hidden:
        return None
    return or_(
        Product.category_id.is_(None),
        Product.category_id.notin_(hidden),
    )


def visible_categories(company_id, module, active_only=True):
    """The categories to offer as tabs/filters inside `module`.

    Without this the POS keeps a tab for a hidden category and it opens
    empty, which reads as a bug rather than as a setting.
    """
    col = _column(module)
    q = ProductCategory.query.filter(
        ProductCategory.company_id == company_id,
        col.is_(True),
    )
    if active_only:
        q = q.filter(ProductCategory.is_active.is_(True))
    return q.order_by(ProductCategory.name).all()


def is_product_visible(product, module):
    """Single-row check, for lookups that resolve one product directly.

    The POS barcode scanner needs this: it finds a variant by barcode and
    has to decide whether to hand it back, and there is no query to bolt a
    clause onto. Uncategorised → visible, matching product_visible_clause.
    """
    if product is None:
        return False
    category = getattr(product, "category", None)
    if category is None:
        return True
    col_name, _label = MODULES[module]
    # A NULL column (partially-applied migration) reads as visible, so a
    # half-migrated database never hides products that were never hidden.
    return getattr(category, col_name, True) is not False


def flags_from_form(form):
    """Read the four checkboxes off a submitted category form.

    An unchecked checkbox is not submitted at all, so presence is the
    signal — `form.get(name)` would read a missing box and a ticked one
    identically once the value is falsy, and unticking would never save.
    """
    return {
        col_name: (col_name in form)
        for col_name, _label in MODULES.values()
    }
