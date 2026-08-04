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
from sqlalchemy import func, or_

from app import db
from app.models import Product, ProductCategory, ProductGroup

# The three states a category control can be in, as posted by the screen.
INHERIT = "inherit"
SHOW = "1"
HIDE = "0"


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


def _col_name(module):
    try:
        col_name, _label = MODULES[module]
    except KeyError:
        raise ValueError(
            f"unknown module {module!r} — expected one of "
            f"{sorted(MODULES)}")
    return col_name


def _effective_col(module):
    """COALESCE(category.<col>, group.<col>) — the resolution rule.

    The category column is a tri-state: NULL means "inherit", so the
    group's value is the fallback. The group column is NOT NULL, so this
    always produces a real answer and no third arm is needed.

    Every caller in this module goes through here. That is the point: the
    rule is written once, and the screen reads it back through
    effective_flags() rather than re-deriving it in a template.
    """
    return func.coalesce(getattr(ProductCategory, _col_name(module)),
                         getattr(ProductGroup, _col_name(module)))


def hidden_category_ids(company_id, module):
    """Ids of this company's categories that resolve to hidden for `module`.

    Returns the HIDDEN set rather than the visible one on purpose. Every
    company starts with all four group flags on and every category
    inheriting, so this is empty, and callers can then skip filtering
    entirely — the guarded query stays byte-for-byte what it was before
    this feature existed. An allow-list would have made the default case a
    large `IN (...)` and any bug in it would silently hide real products.

    Recomputed on every call. There are a handful of categories per
    company, and the ticket requires a change to take effect immediately,
    so nothing is cached.
    """
    rows = db.session.query(ProductCategory.id).join(
        ProductGroup, ProductCategory.group_id == ProductGroup.id,
    ).filter(
        ProductCategory.company_id == company_id,
        _effective_col(module).is_(False),
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
    q = ProductCategory.query.join(
        ProductGroup, ProductCategory.group_id == ProductGroup.id,
    ).filter(
        ProductCategory.company_id == company_id,
        _effective_col(module).is_(True),
    )
    if active_only:
        q = q.filter(ProductCategory.is_active.is_(True))
    return q.order_by(ProductCategory.name).all()


def effective_flag(category, module):
    """Resolve one category + module in Python. Returns (value, inherited).

    The row-level twin of `_effective_col`, for callers holding an object
    rather than building a query — the barcode lookup and the screen.
    """
    col_name = _col_name(module)
    own = getattr(category, col_name, None)
    if own is not None:
        return bool(own), False
    group = getattr(category, "group", None)
    if group is None:
        # An orphaned category cannot inherit; treat as visible rather
        # than silently hiding real products.
        return True, True
    return bool(getattr(group, col_name, True)), True


def effective_flags(category):
    """{module: (value, inherited)} for every module — for the screen.

    Exists so the template never re-implements the COALESCE rule; it asks
    for the answer and renders it.
    """
    return {m: effective_flag(category, m) for m in MODULES}


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
    value, _inherited = effective_flag(category, module)
    return value


def parse_tri_state(raw):
    """'inherit' | '1' | '0' → None | True | False.

    Anything unrecognised means inherit: a control that failed to submit
    should hand the decision back to the group, never invent a hide.
    """
    if raw == SHOW:
        return True
    if raw == HIDE:
        return False
    return None


def category_flags_from_form(form):
    """The four tri-state controls off a submitted category form.

    Unlike the group's checkboxes, these always submit — a <select> has a
    value — so this reads `.get()` rather than presence, and a missing
    field falls back to inherit.
    """
    return {
        _col_name(m): parse_tri_state(form.get(_col_name(m)))
        for m in MODULES
    }


def group_flags_from_form(form):
    """The four checkboxes off a submitted GROUP form.

    An unchecked checkbox is not submitted at all, so presence is the
    signal — `.get()` would read a missing box and a ticked one identically
    once the value is falsy, and unticking would never save.
    """
    return {
        _col_name(m): (_col_name(m) in form)
        for m in MODULES
    }


# Kept so an older caller doesn't break; the group form is the only place
# plain checkboxes remain.
flags_from_form = group_flags_from_form
