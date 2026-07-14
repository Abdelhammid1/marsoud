"""MARSOUD-VENDOR-SUBCAT (Abdelhamid 2026-07-14) — service layer.

Business rules from the ticket:
  · Each sub-category belongs to exactly one vendor.
  · Name is unique within (company, vendor); the same name can be
    reused across vendors (Claude/Rofida and Google/Rofida coexist).
  · Deleting a sub-category referenced by a VendorBillItem is
    forbidden — deactivation is offered instead.
  · Deactivation is always safe (just hides the row from pickers).
  · No accounting impact anywhere in this module.
"""
from datetime import datetime

from app import db
from app.models import VendorSubCategory, VendorBillItem, Vendor


class SubCategoryError(Exception):
    pass


def _normalize_name(raw):
    name = (raw or "").strip()
    if not name:
        raise SubCategoryError("اسم التصنيف مطلوب")
    return name


def _vendor_in_company(vendor_id, company_id):
    v = db.session.get(Vendor, vendor_id)
    if not v or v.company_id != company_id:
        raise SubCategoryError("المورد غير موجود")
    return v


def create_sub_category(*, company_id, vendor_id, name, created_by_id=None):
    """Insert a new sub-category. Raises SubCategoryError on duplicate
    (per the (company, vendor, name) unique key) so the caller can
    show a friendly flash message."""
    name = _normalize_name(name)
    _vendor_in_company(vendor_id, company_id)
    existing = VendorSubCategory.query.filter_by(
        company_id=company_id, vendor_id=vendor_id, name=name,
    ).first()
    if existing:
        raise SubCategoryError(
            f"تصنيف بنفس الاسم موجود بالفعل: {name}")
    row = VendorSubCategory(
        company_id=company_id, vendor_id=vendor_id, name=name,
        is_active=True, created_by_id=created_by_id,
    )
    db.session.add(row); db.session.flush()
    db.session.commit()
    return row


def rename_sub_category(sc, *, name):
    """Edit the display name. Same uniqueness rule as create."""
    new_name = _normalize_name(name)
    if new_name == sc.name:
        return sc
    dup = VendorSubCategory.query.filter(
        VendorSubCategory.company_id == sc.company_id,
        VendorSubCategory.vendor_id == sc.vendor_id,
        VendorSubCategory.name == new_name,
        VendorSubCategory.id != sc.id,
    ).first()
    if dup:
        raise SubCategoryError(
            f"تصنيف آخر بنفس الاسم موجود بالفعل: {new_name}")
    sc.name = new_name
    sc.updated_at = datetime.utcnow()
    db.session.commit()
    return sc


def set_active(sc, active):
    """Deactivate hides from pickers without breaking historical
    bill lines that reference it. Reactivate is always safe."""
    sc.is_active = bool(active)
    sc.updated_at = datetime.utcnow()
    db.session.commit()
    return sc


def is_in_use(sc):
    """True when at least one VendorBillItem points at this
    sub-category. Ticket rule: delete forbidden while in use."""
    return db.session.query(
        db.session.query(VendorBillItem)
        .filter(VendorBillItem.sub_category_id == sc.id)
        .exists()
    ).scalar()


def delete_sub_category(sc):
    """Hard delete. Refuses when the sub-category is referenced by
    any VendorBillItem — the user is prompted to deactivate instead."""
    if is_in_use(sc):
        raise SubCategoryError(
            "لا يمكن الحذف — التصنيف مستخدم في فواتير موجودة. "
            "يمكن إيقافه بدلاً من ذلك."
        )
    db.session.delete(sc)
    db.session.commit()


def list_for_vendor(vendor_id, *, active_only=False):
    q = VendorSubCategory.query.filter_by(vendor_id=vendor_id)
    if active_only:
        q = q.filter_by(is_active=True)
    return q.order_by(VendorSubCategory.name).all()


def report_totals_by_vendor(company_id, vendor_id=None,
                              date_from=None, date_to=None):
    """Group vendor-bill spend by (vendor, sub-category).

    Returns a list of dicts:
        {vendor_id, vendor_name,
         sub_category_id, sub_category_name, total, line_count}

    Uncategorized lines (sub_category_id IS NULL) are collapsed under
    a single "بدون تصنيف" bucket per vendor so the report is
    complete rather than hiding legacy spend.
    """
    from app.models import VendorBill, VendorBillStatus
    from sqlalchemy import func

    q = (
        db.session.query(
            VendorBill.vendor_id.label("vendor_id"),
            Vendor.name.label("vendor_name"),
            VendorBillItem.sub_category_id.label("sub_id"),
            VendorSubCategory.name.label("sub_name"),
            func.coalesce(
                func.sum(VendorBillItem.line_total), 0.0).label("total"),
            func.count(VendorBillItem.id).label("line_count"),
        )
        .join(VendorBill, VendorBillItem.bill_id == VendorBill.id)
        .join(Vendor, VendorBill.vendor_id == Vendor.id)
        .outerjoin(VendorSubCategory,
                    VendorBillItem.sub_category_id == VendorSubCategory.id)
        .filter(VendorBill.company_id == company_id)
        .filter(VendorBill.deleted_at.is_(None))
        # CANCELLED bills should NOT count as spend — they never left
        # the ledger dirty in the first place.
        .filter(VendorBill.status != VendorBillStatus.CANCELLED)
    )
    if vendor_id:
        q = q.filter(VendorBill.vendor_id == vendor_id)
    if date_from:
        q = q.filter(VendorBill.issue_date >= date_from)
    if date_to:
        q = q.filter(VendorBill.issue_date <= date_to)
    q = q.group_by(
        VendorBill.vendor_id, Vendor.name,
        VendorBillItem.sub_category_id, VendorSubCategory.name,
    ).order_by(Vendor.name, VendorSubCategory.name.nullslast())
    rows = []
    for r in q.all():
        rows.append({
            "vendor_id": r.vendor_id,
            "vendor_name": r.vendor_name,
            "sub_category_id": r.sub_id,
            "sub_category_name": r.sub_name or "بدون تصنيف",
            "total": float(r.total or 0),
            "line_count": int(r.line_count or 0),
        })
    return rows
