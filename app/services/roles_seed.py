"""MARSOUD-32 — seed permissions catalogue + system roles per company.

The catalogue (PERMISSION_CATALOG) is the source of truth for every action
that can appear on the permission grid. Each entry maps a P-dict code →
(group_ar, label_ar). Adding a new permission is one entry here + an
update to the P dict in permissions.py.

System roles are seeded from app.services.permissions.P (so this stays
the canonical mapping until the DB rewrite lands). For each company we
create one Role row per name in ALL_ROLES, attach the matching set of
permissions, and link existing user_companies rows to the right role_id.
"""
from app import db
from app.models import Role, Permission
from app.models.user import user_companies
from app.services.permissions import P, ALL_ROLES, ROLE_LABELS_AR


# ─── Catalogue ──────────────────────────────────────────────────────────
# code → (group_ar, label_ar, action_kind)
# action_kind is one of: view, add, edit, delete, run — used to bucket
# entries into the 4-column matrix when possible.
PERMISSION_CATALOG = {
    # ─── المالية والمحاسبة ──────────────────────────────────────────────
    "invoices.create":      ("المالية والمحاسبة", "الفواتير: إنشاء + إرسال", "add"),
    "invoices.send":        ("المالية والمحاسبة", "الفواتير: إرسال", "edit"),
    "invoices.refund":      ("المالية والمحاسبة", "الفواتير: استرداد / إلغاء", "delete"),

    "journals.view":        ("المالية والمحاسبة", "القيود اليومية: عرض الدفتر", "view"),
    "journals.create":      ("المالية والمحاسبة", "القيود اليومية: إنشاء", "add"),
    "journals.pause":       ("المالية والمحاسبة", "القيود اليومية: إيقاف", "edit"),
    "journals.reverse":     ("المالية والمحاسبة", "القيود اليومية: قيد عكسي", "delete"),
    "journals.recurring":   ("المالية والمحاسبة", "القيود اليومية: المتكررة", "edit"),

    "vendor_bills.create":  ("المالية والمحاسبة", "فواتير الموردين: إنشاء", "add"),
    "vendor_bills.delete":  ("المالية والمحاسبة", "فواتير الموردين: حذف (DRAFT فقط)", "delete"),

    "accounts.manage":      ("المالية والمحاسبة", "شجرة الحسابات: إدارة كاملة", "edit"),
    "partners.manage":      ("المالية والمحاسبة", "العملاء والموردون: إدارة", "edit"),
    "products.manage":      ("المالية والمحاسبة", "المنتجات والخدمات: إدارة", "edit"),
    "payment_methods.manage": ("المالية والمحاسبة", "طرق الدفع: إدارة", "edit"),
    "assets.manage":        ("المالية والمحاسبة", "الأصول الثابتة: إدارة", "edit"),
    # MARSOUD-ASSET-DISPOSAL-01 (2026-08-07) — split off .manage so
    # a custom role can hold "read + monthly depreciate" without
    # also holding the irreversible disposal button.
    "assets.dispose":       ("المالية والمحاسبة",
                              "شطب / استبعاد أصل ثابت (عملية لا رجعة فيها)",
                              "delete"),

    "reports.view":         ("المالية والمحاسبة", "التقارير المالية: عرض", "view"),
    "reports.export":       ("المالية والمحاسبة", "التقارير المالية: تصدير", "edit"),

    # ─── الموارد البشرية ────────────────────────────────────────────────
    "payroll.run":          ("الموارد البشرية", "الرواتب: تشغيل كشف", "run"),
    "payroll.employees":    ("الموارد البشرية", "الموظفون: إدارة كاملة", "edit"),
    "payroll.accruals":     ("الموارد البشرية", "الرواتب: سداد المستحقات", "delete"),
    "payroll.view":         ("الموارد البشرية", "الرواتب: عرض الأرقام (الراتب والمسيّرات)", "view"),
    "employees.view":       ("الموارد البشرية", "الموظفون: عرض البيانات (بدون أرقام الرواتب)", "view"),
    "hr.manage":            ("الموارد البشرية", "HR: أقسام + بيانات الموظف", "edit"),
    # MARSOUD-EVALUATIONS-PRO-GATING (Batch 5 Ticket 6, 2026-07-29).
    "evaluations.manage":   ("الموارد البشرية", "تقييم الأداء: دورات + أهداف + مكافآت", "edit"),
    # MARSOUD-ADVANCES (2026-08-03) — approving an advance disburses cash
    # and posts a journal, so it sits with the financial permissions
    # (same role list as payroll.accruals), not with hr.manage.
    "advances.manage":      ("الموارد البشرية", "السلف: اعتماد + إضافة مباشرة + إلغاء", "edit"),
    # MARSOUD-CASH-CUSTODY-01 (2026-08-07) — cash custody: separate
    # from advances (advances deducted from salary; custody closed
    # by receipts + rest returned).
    "custody.manage":       ("العهد النقدية",
                              "العهد النقدية: اعتماد + صرف + إقفال + إلغاء",
                              "edit"),
    "custody.request":      ("العهد النقدية",
                              "طلب عهدة نقدية جديدة من البورتال",
                              "create"),

    # ─── العمليات والمبيعات ─────────────────────────────────────────────
    "leads.view":           ("العمليات والمبيعات", "العملاء المحتملون: عرض المسند لي فقط", "view"),
    "leads.view_all":       ("العمليات والمبيعات", "العملاء المحتملون: عرض الكل (للمدير)", "view"),
    "leads.manage":         ("العمليات والمبيعات", "العملاء المحتملون: إدارة + إسناد", "edit"),
    "leads.convert":        ("العمليات والمبيعات", "العملاء المحتملون: تحويل لمشروع", "edit"),
    "leads.delete":         ("العمليات والمبيعات", "العملاء المحتملون: حذف", "delete"),

    "projects.view":        ("العمليات والمبيعات", "المشاريع: عرض مشاريعي فقط", "view"),
    "projects.view_all":    ("العمليات والمبيعات", "المشاريع: عرض كل المشاريع (للمدير)", "view"),
    "projects.create":      ("العمليات والمبيعات", "المشاريع: إنشاء", "add"),
    "projects.manage":      ("العمليات والمبيعات", "المشاريع: إدارة", "edit"),
    # MARSOUD-PROJECT-ARCHIVE (2026-08-10).
    "projects.archive":     ("العمليات والمبيعات", "المشاريع: أرشفة/استعادة", "delete"),

    "tasks.view":           ("العمليات والمبيعات", "المهام: عرض المسند لي فقط", "view"),
    "tasks.view_all":       ("العمليات والمبيعات", "المهام: عرض كل مهام الشركة (للمدير)", "view"),
    "tasks.manage":         ("العمليات والمبيعات", "المهام: إدارة", "edit"),
    "tasks.delete":         ("العمليات والمبيعات", "المهام: حذف نهائي", "delete"),
    "tasks.archive":        ("العمليات والمبيعات", "المهام: أرشفة واستعادة", "edit"),

    "customers.view":       ("المالية والمحاسبة", "العملاء: عرض القائمة والتفاصيل", "view"),

    "inventory.view":       ("العمليات والمبيعات", "المخزون: عرض الأرصدة والسجل", "view"),
    "inventory.manage":     ("العمليات والمبيعات", "المخزون: تسويات + رصيد افتتاحي + مخازن", "edit"),

    "pos.use":              ("العمليات والمبيعات", "نقطة البيع: تسجيل أوردرات", "add"),
    "pos.void":             ("العمليات والمبيعات", "نقطة البيع: إلغاء أوردر", "delete"),
    "reports.profitability": ("المالية والمحاسبة", "تقرير ربحية المنتجات", "view"),
    "reports.cashier_sales": ("المالية والمحاسبة", "تقرير مبيعات الكاشير", "view"),
    "transfers.view":       ("العمليات والمبيعات", "تحويلات المخازن: عرض", "view"),
    "transfers.manage":     ("العمليات والمبيعات", "تحويلات المخازن: إنشاء + تنفيذ", "edit"),
    "shifts.manage":        ("العمليات والمبيعات", "ورديات الكاشير: فتح وإغلاق", "edit"),

    # ─── النظام ─────────────────────────────────────────────────────────
    "users.manage":         ("النظام", "المستخدمون: دعوة وإدارة", "edit"),
    "users.view":           ("النظام", "المستخدمون: عرض", "view"),
    "company.edit":         ("النظام", "إعدادات الشركة", "edit"),
    "company.create":       ("النظام", "إنشاء شركة جديدة", "add"),
    "agent.use":            ("النظام", "المحاسب الذكي (AI)", "run"),
    # MARSOUD-AGENT-SAFETY-03 (2026-08-06) — separate grant so a
    # company can give a staff member READ-ONLY agent access
    # (agent.use without agent.write). Without this catalog entry the
    # DB-backed permission check returns False for owner/admin/
    # accountant on the .write action, blocking the proposal-execute
    # route even though the legacy P-dict grants it.
    "agent.write":          ("النظام", "المحاسب الذكي — كتابة", "run"),
    "insights.use":         ("النظام", "المحلل الذكي (تحليلات وقراءة)", "run"),

    # ─── MARSOUD-PERM-EXPAND — new sidebar items (per-endpoint) ─────────
    # These live on top of the umbrella permissions above and are IMPLIED
    # by them (see _IMPLIES in permissions.py), so ticking the umbrella
    # is enough — owners only need these entries when they want to
    # RESTRICT a specific endpoint without touching the umbrella.
    "crm.campaigns.view":   ("العمليات والمبيعات", "CRM: الحملات التسويقية", "view"),
    "crm.activities.view":  ("العمليات والمبيعات", "CRM: الأنشطة والمتابعات", "view"),
    "crm.contacts.view":    ("العمليات والمبيعات", "CRM: جهات الاتصال", "view"),
    "crm.analytics.view":   ("العمليات والمبيعات", "CRM: تحليلات المسار البيعي", "view"),
    "party_ledger.view":    ("المالية والمحاسبة", "كشف حساب طرف (عميل/مورد/موظف)", "view"),

    # ─── MARSOUD-OPS-FOUNDATION §6 ──────────────────────────────────────
    # One gate for every wizard meant granting the till-to-bank transfer
    # also granted capital injections. Implied by journals.create (see
    # _IMPLIES) so existing roles keep working untouched; these entries
    # exist so an owner can RESTRICT one wizard on its own.
    "ops.transfer":         ("المالية والمحاسبة", "العمليات المحاسبية: تحويل بين الحسابات المالية", "add"),
    "ops.accruals":         ("المالية والمحاسبة", "العمليات المحاسبية: إثبات الالتزامات المستحقة", "add"),
    "ops.settle":           ("المالية والمحاسبة", "العمليات المحاسبية: سداد الالتزامات المستحقة", "edit"),
    "ops.adjustments":      ("المالية والمحاسبة", "العمليات المحاسبية: تسوية أرصدة الحسابات (خطير)", "edit"),
    "api_tokens.manage":    ("النظام", "مفاتيح الـ API: إدارة", "edit"),
    "activity_log.view":    ("النظام", "سجل نشاط الموظفين", "view"),
    "backup.download":      ("النظام", "نسخة احتياطية (Excel)", "run"),

    # ─── MARSOUD-REFUNDS-01 ─────────────────────────────────────────────
    "refunds.view":         ("المالية والمحاسبة", "المرتجعات: عرض الصفحة والتقرير", "view"),
    "refunds.manage":       ("المالية والمحاسبة", "المرتجعات: إنشاء (مبيعات + مشتريات)", "edit"),
    "vendor_bills.refund":  ("المالية والمحاسبة", "فواتير الموردين: مرتجعات", "delete"),

    # ─── MARSOUD-EMPLOYEE-DAILY-REPORTS ─────────────────────────────
    "employee_reports.view": ("النظام", "تقارير الموظفين اليومية: عرض", "view"),

    # ─── MARSOUD-MANUFACTURING-01 ───────────────────────────────────
    "manufacturing.view":     ("العمليات والمبيعات", "التصنيع: عرض", "view"),
    "manufacturing.manage":   ("العمليات والمبيعات", "التصنيع: إدارة BOM وأوامر الإنتاج", "edit"),
    "manufacturing.complete": ("العمليات والمبيعات", "التصنيع: إكمال أمر إنتاج (سحب مواد + قيد)", "delete"),

    # ─── MARSOUD-SIDEBAR-COMPLETE ───────────────────────────────────
    "settings_usage.view":    ("النظام", "استهلاك الباقة", "view"),
    "companies.manage":       ("النظام", "إدارة الشركات المتعددة", "edit"),

    # ─── MARSOUD-SUPPORT-TICKETS-01 ─────────────────────────────────
    "support.manage_tickets": ("النظام", "الدعم الفني: إدارة كل تذاكر منصتي", "edit"),
}


def seed_permissions_catalog():
    """Insert any catalogue entries not already in `permissions`. Idempotent."""
    existing = {p.code for p in Permission.query.all()}
    added = 0
    for code, (group_ar, label_ar, action_kind) in PERMISSION_CATALOG.items():
        if code in existing:
            continue
        # Derive resource + action from the code (e.g. "invoices.create" → "invoices", "create")
        resource, _, action = code.partition(".")
        db.session.add(Permission(
            code=code, resource=resource, action=action,
            group_ar=group_ar, label_ar=label_ar,
        ))
        added += 1
    if added:
        db.session.commit()
    return added


def seed_system_roles_for_company(company_id):
    """Create the system roles for a company (idempotent) + attach their
    canonical permission sets from app.services.permissions.P.
    """
    if not Permission.query.first():
        seed_permissions_catalog()

    existing = {r.code: r for r in Role.query.filter_by(company_id=company_id).all()}
    created = 0
    for code in ALL_ROLES:
        name_ar = ROLE_LABELS_AR.get(code, code)
        if code in existing:
            role = existing[code]
            role.type = "SYSTEM"
            role.name_ar = name_ar
        else:
            role = Role(
                company_id=company_id, code=code, name_ar=name_ar,
                type="SYSTEM",
                description=f"دور النظام: {name_ar}",
            )
            db.session.add(role)
            db.session.flush()
            created += 1
        # Always re-sync permissions from the P dict — so newly-added
        # permission codes flow into existing system roles on next boot
        # without needing a manual migration. Custom roles are left alone.
        granted_codes = {
            permission_code
            for permission_code, allowed_roles in P.items()
            if code in allowed_roles
        }
        if granted_codes:
            granted_perms = Permission.query.filter(
                Permission.code.in_(granted_codes)
            ).all()
            role.permissions = granted_perms
        else:
            role.permissions = []
    db.session.commit()
    return created


def backfill_user_companies_role_ids(company_id):
    """For every user_companies row in this company, set role_id to the
    matching system role's id based on the string `role` value.

    Safe to re-run — only updates rows where role_id is currently NULL.
    """
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == company_id)
    ).fetchall()
    code_to_id = {
        r.code: r.id
        for r in Role.query.filter_by(company_id=company_id).all()
    }
    updated = 0
    for row in rows:
        if row.role_id is not None:
            continue
        role_id = code_to_id.get(row.role)
        if role_id is None:
            continue  # unknown role string — leave NULL, will fall back to string
        db.session.execute(
            user_companies.update().where(
                (user_companies.c.user_id == row.user_id) &
                (user_companies.c.company_id == company_id)
            ).values(role_id=role_id)
        )
        updated += 1
    if updated:
        db.session.commit()
    return updated


def ensure_roles_ready_for_company(company_id):
    """One-shot setup wrapper: seed permissions catalogue if needed,
    seed system roles for this company, then backfill user_companies.

    Cheap to call repeatedly — every sub-step is idempotent.
    """
    seed_permissions_catalog()
    seed_system_roles_for_company(company_id)
    return backfill_user_companies_role_ids(company_id)
