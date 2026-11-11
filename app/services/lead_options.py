"""Per-company option lists for the CRM (systems, industries, idea stage,
unqualified reasons). Defaults are seeded lazily the first time a kind is read
for a company. Values are stored on the lead as label text (JSON list for
multi-select) so deactivating an option never rewrites old leads."""
import json

from sqlalchemy.exc import IntegrityError

from app import db
from app.models.crm import LeadOption

KIND_LABELS_AR = {
    "system": "السيستم المطلوب",
    "industry": "مجال العميل",
    "idea_stage": "حالة الفكرة",
    "unqualified_reason": "أسباب غير مؤهل",
}
KINDS = tuple(KIND_LABELS_AR)

DEFAULTS = {
    "system": [
        "مرصود (محاسبة / ERP)", "المحامي (LexOffice)", "المستشار",
        "ActiveFit (جيم)", "نظام مدارس", "نظام مطاعم",
        "Web Application", "Mobile Application", "موقع تعريفي / Landing",
        "Chatbot / AI Agent", "نظام مخصص (Custom)", "أخرى",
    ],
    "industry": [
        "تعليم", "مقاولات", "مطاعم", "كافيهات", "تجارة وتوزيع", "تصنيع",
        "عيادات ومستشفيات", "محاماة", "عقارات", "لوجستيات وتوصيل",
        "رياضة ولياقة", "زراعة ومزارع", "خدمات", "تقنية", "أخرى",
    ],
    "idea_stage": ["فكرة جديدة", "نشاط قائم"],
    "unqualified_reason": [
        "الميزانية غير مناسبة", "جاي بالغلط / مش مهتم",
        "بيدور على خدمة مش بنقدمها", "طالب مجاني / تجربة بس",
        "مش صاحب قرار", "مجرد استفسار / بيقارن أسعار",
        "رقم أو بيانات غير صحيحة", "خارج السوق الجغرافي بتاعنا",
        "تكرار (عميل موجود)", "حجم النشاط صغير جدًا",
    ],
}


def ensure_seeded(company_id, kind):
    exists = db.session.query(LeadOption.id).filter_by(
        company_id=company_id, kind=kind).first()
    if exists:
        return
    for i, label in enumerate(DEFAULTS.get(kind, [])):
        db.session.add(LeadOption(company_id=company_id, kind=kind,
                                  label=label, sort_order=i, is_active=True))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # another request seeded first


def get_options(company_id, kind, include_inactive=False):
    ensure_seeded(company_id, kind)
    q = LeadOption.query.filter_by(company_id=company_id, kind=kind)
    if not include_inactive:
        q = q.filter_by(is_active=True)
    return q.order_by(LeadOption.sort_order, LeadOption.id).all()


def active_labels(company_id, kind):
    return [o.label for o in get_options(company_id, kind)]


def options_for_form(company_id, kind, selected=()):
    labels = active_labels(company_id, kind)
    return labels + [s for s in selected if s and s not in labels]


def parse_selected(company_id, kind, raw_values, keep=()):
    allowed = options_for_form(company_id, kind, keep)
    wanted = {(v or "").strip() for v in (raw_values or [])}
    return [a for a in allowed if a in wanted]


def parse_single(company_id, kind, raw, keep=None):
    raw = (raw or "").strip()
    if not raw:
        return None
    allowed = options_for_form(company_id, kind, [keep] if keep else [])
    return raw if raw in allowed else None


def dumps_list(items):
    return json.dumps(list(items), ensure_ascii=False) if items else None


def join_labels(items, limit=200):
    return "، ".join(items)[:limit]
