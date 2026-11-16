"""MARSOUD-P101 — Industry Presets & Per-Tenant Feature Configuration.

Allows Super-Admin to configure a tenant company for a specific business
vertical (Apparel, Grocery/Food, Mobiles/Electronics, Cosmetics, Services,
or General Trade) in one click:
  1. Stores `industry_code` and `industry_features` JSON on `Company`.
  2. Applies clean MODULE / SUBITEM overrides via `company_overrides`
     (replacing any prior `[قالب مجال]` overrides so switching industries
     never leaves stale restrictions behind).
"""
import json
from app import db
from app.models import CompanyFeatureOverride
from app.services.company_overrides import upsert_override, _invalidate_for_company

PRESET_REASON_PREFIX = "[قالب مجال]"

DEFAULT_FEATURES = {
    "variants": True,       # مقاسات / ألوان / درجات
    "multi_units": True,    # وحدات قياس متعددة بمعامل تحويل
    "expiry_lots": True,    # تواريخ صلاحية ورقم تشغيلة
    "serial_imei": True,    # أرقام تسلسلية IMEI / Serial
    "barcodes": True,       # طباعة ومسح باركود
    "pos_fast": True,       # شاشة نقطة البيع السريعة
    "bundles": True,        # باقات وعروض مجمعة
}

INDUSTRY_PRESETS = {
    "general": {
        "code": "general",
        "label_ar": "تجارة عامة (قياسي)",
        "icon": "🏢",
        "description_ar": "إعداد قياسي مناسب للأنشطة التجارية المتنوعة مع إتاحة كافة الخصائص.",
        "features": {
            "variants": True,
            "multi_units": True,
            "expiry_lots": True,
            "serial_imei": True,
            "barcodes": True,
            "pos_fast": True,
            "bundles": True,
        },
        "module_overrides": {},
        "subitem_overrides": {},
    },
    "apparel": {
        "code": "apparel",
        "label_ar": "ملابس وأحذية",
        "icon": "👕",
        "description_ar": "مقاسات وألوان (Variants) + باركود لكل قطعة + مرتجعات واستبدال، مع إخفاء الصلاحية والسيريال والتصنيع.",
        "features": {
            "variants": True,
            "multi_units": False,
            "expiry_lots": False,
            "serial_imei": False,
            "barcodes": True,
            "pos_fast": True,
            "bundles": True,
        },
        "module_overrides": {
            "manufacturing": "DENY",
            "inventory": "GRANT",
            "pos": "GRANT",
        },
        "subitem_overrides": {
            "inventory.barcodes_picker": "GRANT",
        },
    },
    "grocery": {
        "code": "grocery",
        "label_ar": "أغذية وبقالة وسوبرماركت",
        "icon": "🛒",
        "description_ar": "وحدات متعددة (قطعة/علبة/كرتونة/كيلو) + تواريخ صلاحية وتشغيلات + باركود + كاشير سريع، مع إخفاء السيريال والمقاسات.",
        "features": {
            "variants": False,
            "multi_units": True,
            "expiry_lots": True,
            "serial_imei": False,
            "barcodes": True,
            "pos_fast": True,
            "bundles": True,
        },
        "module_overrides": {
            "manufacturing": "DENY",
            "inventory": "GRANT",
            "pos": "GRANT",
        },
        "subitem_overrides": {
            "inventory.barcodes_picker": "GRANT",
            "inventory_counts.index": "GRANT",
        },
    },
    "mobiles": {
        "code": "mobiles",
        "label_ar": "موبايلات وإلكترونيات",
        "icon": "📱",
        "description_ar": "أرقام تسلسلية (IMEI / Serial) لكل جهاز + ضمان + باركود + مرتجعات، مع إخفاء تواريخ الصلاحية والمقاسات والتصنيع.",
        "features": {
            "variants": False,
            "multi_units": False,
            "expiry_lots": False,
            "serial_imei": True,
            "barcodes": True,
            "pos_fast": True,
            "bundles": True,
        },
        "module_overrides": {
            "manufacturing": "DENY",
            "inventory": "GRANT",
            "pos": "GRANT",
        },
        "subitem_overrides": {
            "inventory.barcodes_picker": "GRANT",
        },
    },
    "cosmetics": {
        "code": "cosmetics",
        "label_ar": "مستحضرات تجميل وعطور",
        "icon": "💄",
        "description_ar": "درجات وأحجام (Variants) + تواريخ صلاحية + باقات وعروض + باركود، مع إخفاء السيريال والتصنيع.",
        "features": {
            "variants": True,
            "multi_units": False,
            "expiry_lots": True,
            "serial_imei": False,
            "barcodes": True,
            "pos_fast": True,
            "bundles": True,
        },
        "module_overrides": {
            "manufacturing": "DENY",
            "inventory": "GRANT",
            "pos": "GRANT",
        },
        "subitem_overrides": {
            "inventory.barcodes_picker": "GRANT",
        },
    },
    "services": {
        "code": "services",
        "label_ar": "خدمات ومكاتب واستشارات",
        "icon": "💼",
        "description_ar": "واجهة مبسطة للخدمات والفواتير والعملاء والمالية مع إخفاء المخازن والباركود ونقطة البيع والتصنيع.",
        "features": {
            "variants": False,
            "multi_units": False,
            "expiry_lots": False,
            "serial_imei": False,
            "barcodes": False,
            "pos_fast": False,
            "bundles": False,
        },
        "module_overrides": {
            "manufacturing": "DENY",
            "pos": "DENY",
        },
        "subitem_overrides": {
            "inventory.index": "DENY",
            "inventory.warehouses": "DENY",
            "inventory.adjust": "DENY",
            "inventory.opening_balance": "DENY",
            "inventory.movements": "DENY",
            "inventory.transfers": "DENY",
            "inventory.inventory_balance": "DENY",
            "inventory.barcodes_picker": "DENY",
            "inventory_counts.index": "DENY",
        },
    },
}

FEATURE_LABELS_AR = {
    "variants": "المقاسات والألوان/الدرجات",
    "multi_units": "وحدات القياس المتعددة",
    "expiry_lots": "تواريخ الصلاحية والدفعات",
    "serial_imei": "الأرقام التسلسلية (IMEI / Serial)",
    "barcodes": "الباركود وطباعة الملصقات",
    "pos_fast": "نقطة البيع السريعة (POS)",
    "bundles": "الباقات والعروض المجمعة",
}


def get_preset(code):
    return INDUSTRY_PRESETS.get((code or "general").strip().lower(), INDUSTRY_PRESETS["general"])


def list_presets():
    return list(INDUSTRY_PRESETS.values())


def parse_company_features(company):
    preset = get_preset(getattr(company, "industry_code", None))
    merged = dict(DEFAULT_FEATURES)
    merged.update(preset["features"])
    raw = getattr(company, "industry_features", None)
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if k in merged:
                        merged[k] = bool(v)
        except Exception:
            pass
    return merged


def apply_industry_preset(company, industry_code, actor_id=None, custom_features=None):
    """Apply an industry preset to `company` in one atomic operation."""
    preset = get_preset(industry_code)
    code = preset["code"]

    features = dict(preset["features"])
    if isinstance(custom_features, dict):
        for k in DEFAULT_FEATURES:
            if k in custom_features:
                features[k] = bool(custom_features[k])

    company.industry_code = code
    company.industry_features = json.dumps(features, ensure_ascii=False)

    # Remove previous preset-managed overrides so switching presets is clean
    stale = CompanyFeatureOverride.query.filter(
        CompanyFeatureOverride.company_id == company.id,
        CompanyFeatureOverride.reason.like(f"{PRESET_REASON_PREFIX}%"),
    ).all()
    for row in stale:
        db.session.delete(row)
    db.session.commit()
    _invalidate_for_company(company.id)

    reason = f"{PRESET_REASON_PREFIX} {preset['label_ar']}"
    for mod_code, mode in preset.get("module_overrides", {}).items():
        try:
            upsert_override(
                company.id, mod_code, mode, reason,
                actor_id=actor_id, scope="MODULE",
            )
        except ValueError:
            pass

    for ep_code, mode in preset.get("subitem_overrides", {}).items():
        try:
            upsert_override(
                company.id, ep_code, mode, reason,
                actor_id=actor_id, scope="SUBITEM",
            )
        except ValueError:
            pass

    try:
        from app.services.superadmin import log_platform_action
        log_platform_action(
            "company_industry_preset",
            target_company_id=company.id,
            actor_id=actor_id,
            details=f"industry={code} ({preset['label_ar']})",
        )
    except Exception:
        pass

    return preset, features
