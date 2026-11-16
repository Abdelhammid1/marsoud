"""MARSOUD-P111 — Live per-company onboarding & configuration context for the Guide Agent."""
from __future__ import annotations
import json


def build_guide_context(company, current_path: str = "") -> str:
    """Build a compact, real-time snapshot of the company's setup state and current screen."""
    if not company:
        return "لا توجد شركة نشطة حالياً."

    from app import db
    from app.models import Account, Customer, Vendor, Product, Invoice, VendorBill
    from app.services.industry_presets import INDUSTRY_PRESETS, DEFAULT_FEATURES
    from app.services.plan_gating import plan_allows
    from app.services.feature_registry import all_modules

    lines = [
        f"اسم الشركة: {company.name}",
        f"العملة الأساسية: {company.base_currency or 'SAR'} | الضريبة الافتراضية: {getattr(company, 'vat_rate', 15)}%",
    ]

    if current_path:
        lines.append(f"الشاشة المفتوحة حالياً أمام المستخدم: {current_path}")

    # Industry preset & active product features
    ind_code = getattr(company, "industry_code", None) or "general"
    preset = INDUSTRY_PRESETS.get(ind_code, INDUSTRY_PRESETS.get("general", {}))
    raw_feats = getattr(company, "industry_features", None)
    feats = dict(DEFAULT_FEATURES)
    if raw_feats:
        try:
            parsed = json.loads(raw_feats) if isinstance(raw_feats, str) else raw_feats
            if isinstance(parsed, dict):
                feats.update(parsed)
        except Exception:
            pass
    active_feats = [k for k, v in feats.items() if v]
    lines.append(f"قالب النشاط التجاري: {preset.get('label_ar', ind_code)} ({ind_code})")
    lines.append(f"خصائص الأصناف المفعّلة: {', '.join(active_feats) if active_feats else 'قياسي'}")

    # Enabled modules in this company's plan
    enabled_mods = []
    for m in all_modules(include_core=True):
        try:
            if m.is_core or plan_allows(company, m.code):
                enabled_mods.append(f"{m.label_ar} ({m.code})")
        except Exception:
            pass
    if enabled_mods:
        lines.append("الوحدات المفعّلة في باقة الشركة: " + " · ".join(enabled_mods))

    # Live setup readiness counts
    cid = company.id
    try:
        acc_count = Account.query.filter_by(company_id=cid, is_active=True).count()
        cust_count = Customer.query.filter_by(company_id=cid).count()
        vend_count = Vendor.query.filter_by(company_id=cid).count()
        prod_count = Product.query.filter_by(company_id=cid).count()
        inv_count = Invoice.query.filter_by(company_id=cid).count()
        bill_count = VendorBill.query.filter_by(company_id=cid).count()
    except Exception:
        acc_count = cust_count = vend_count = prod_count = inv_count = bill_count = -1

    emp_count = 0
    wh_count = 0
    try:
        from app.models import Employee
        emp_count = Employee.query.filter_by(company_id=cid).count()
    except Exception:
        pass
    try:
        from app.models import Warehouse
        wh_count = Warehouse.query.filter_by(company_id=cid).count()
    except Exception:
        pass

    lines.append(
        f"حالة بيانات الشركة الفعلية: "
        f"الحسابات={acc_count} | المستودعات={wh_count} | المنتجات/الخدمات={prod_count} | "
        f"العملاء={cust_count} | الموردون={vend_count} | فواتير المبيعات={inv_count} | "
        f"فواتير المشتريات={bill_count} | الموظفون={emp_count}"
    )

    return "\n".join(lines)
