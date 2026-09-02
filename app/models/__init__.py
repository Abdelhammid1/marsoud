from app.models.user import User, user_companies, UserStatus
from app.models.company import Company
from app.models.plan import (
    Plan, PlanPrice, SubscriptionReminderSent, DEFAULT_CURRENCY,
)
# MARSOUD-CONSENT-AUDIT-LOG (Abdelhamid 2026-07-22).
from app.models.consent import ConsentEvent
# MARSOUD-FEATURE-FLAGS-KILL-SWITCH (Abdelhamid 2026-07-22).
from app.models.feature_flag import FeatureFlag
# MARSOUD-SUPERADMIN-CONTROL-01 T4 (2026-08-08).
from app.models.company_feature_override import CompanyFeatureOverride
# MARSOUD-DISCOUNT-COUPONS (Abdelhamid 2026-07-22).
from app.models.coupon import (
    Coupon, CouponRedemption,
    DISCOUNT_PERCENT, DISCOUNT_FIXED,
)
# MARSOUD-CUSTOMER-BROADCAST-CENTER (Abdelhamid 2026-07-22).
from app.models.broadcast import (
    Broadcast,
    AUDIENCE_ALL, AUDIENCE_TRIAL, AUDIENCE_ACTIVE, AUDIENCE_EXPIRED,
    AUDIENCE_BY_PLAN,
)
# MARSOUD-HELP-CENTER-01 (Abdelhamid 2026-07-24).
from app.models.help import (
    HelpArticle, HelpArticleExample, HelpArticleMedia,
    MEDIA_IMAGE, MEDIA_YOUTUBE, MEDIA_VIMEO, MEDIA_LINK,
)
# MARSOUD-DUAL-UOM-WEIGHT-01 (Abdelhamid 2026-07-24).
from app.models.inventory import (
    InventoryCount, INV_COUNT_DRAFT, INV_COUNT_CONFIRMED,
)
# MARSOUD-INSTALLMENT-PLAN-01 (Abdelhamid 2026-07-24).
from app.models.invoice_installment import (
    InvoiceInstallment, InstallmentReminderSent,
    INSTALLMENT_PENDING, INSTALLMENT_PAID, INSTALLMENT_OVERDUE,
    ALL_INSTALLMENT_STATUSES, INSTALLMENT_STATUS_LABELS_AR,
)
# MARSOUD-CUSTOMER-DEPOSIT-01 (Abdelhamid 2026-07-24).
from app.models.customer_deposit import (
    CustomerDeposit,
    DEPOSIT_ACTIVE, DEPOSIT_APPLIED, DEPOSIT_REFUNDED,
    ALL_DEPOSIT_STATUSES, DEPOSIT_STATUS_LABELS_AR,
)
# MARSOUD-CALENDAR-MANUAL-EVENTS (Abdelhamid 2026-07-29).
from app.models.calendar_event import CalendarEvent
# MARSOUD-RECURRING-INVOICE-01 (Abdelhamid 2026-07-24).
from app.models.recurring_invoice import (
    RecurringInvoice, RecurringInvoiceLog,
    REC_INV_ACTION_EXECUTE, REC_INV_ACTION_FAIL,
    REC_INV_ACTION_STOP, REC_INV_ACTION_RESUME,
    REC_INV_ACTION_DELETE,
    REC_INV_FREQ_DAILY, REC_INV_FREQ_WEEKLY,
    REC_INV_FREQ_MONTHLY, REC_INV_FREQ_YEARLY,
    ALL_REC_INV_FREQS, REC_INV_FREQ_LABELS_AR,
)
# MARSOUD-SUPPORT-TICKETS-01 (Abdelhamid 2026-07-24).
from app.models.support import (
    SupportTicket, SupportTicketComment, SupportTicketAudit,
    STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_WAITING,
    STATUS_RESOLVED, STATUS_CLOSED, ALL_STATUSES, STATUS_LABELS_AR,
    PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH, PRIORITY_URGENT,
    ALL_PRIORITIES, PRIORITY_LABELS_AR,
    ACTION_REPLY, ACTION_INTERNAL, ACTION_STATUS,
    ACTION_PRIORITY, ACTION_ASSIGN,
)
from app.models.platform_setting import PlatformSetting
from app.models.recurring_bill import (
    RecurringBill, RecurringBillOverride,
    INTERVAL_UNITS, OVERRIDE_ACTIONS,
)
from app.models.sales_commission import SalesCommission, COMMISSION_STATUSES
from app.models.account import Account, AccountType, NormalSide
from app.models.journal import JournalEntry, JournalLine
from app.models.journal_extras import (
    JournalAudit, JournalAction, JournalTemplate, JournalTemplateLine,
    RecurringJournal, RecurrenceFrequency,
    RecurringJournalLog, RecurringAction,
)
from app.models.product import Product, ProductGroup, ProductCategory, ProductUnit
from app.models.payment_method import PaymentMethod
from app.models.invoice import Invoice, InvoiceItem, InvoiceStatus, Payment, DiscountType, InvoiceReminderSent
from app.models.partner import Customer, Vendor, CustomerComment, CustomerNote
from app.models.asset import FixedAsset, DepreciationEntry, DisposalReason
from app.models.vendor_bill import (
    VendorBill, VendorBillItem, VendorBillPayment,
    VendorBillStatus, VendorBillPaymentMethod, BillLineType,
)
from app.models.vendor_sub_category import VendorSubCategory
from app.models.payroll import (
    Employee, PayrollRun, PayrollLine, EmployeeAccrual,
    ContractType, EmployeeStatus, TerminationReason, Gender,
    EmployeeHistory, EmployeeChangeType,
)
from app.models.hr_decision import (
    HrDecision, HrDecisionKind, HrDecisionStatus, HrDecisionTiming,
    kind_category as hr_decision_category,
)
from app.models.open_item import (
    OpenItem, OpenItemSettlement, OpenItemStatus, SETTLEABLE_STATUSES,
)
from app.models.advances import (
    AdvanceRequest, AdvanceRequestStatus,
    EmployeeAdvance, AdvanceStatus, AdvanceSource, AdvanceRepayment,
)
# MARSOUD-CASH-CUSTODY-01 (2026-08-07) — separate from advances by
# design: custody is closed by receipts, not deducted from salary.
from app.models.cash_custody import (
    CashCustodyRequest, CashCustody, CashCustodySettlementLine,
    CustodyHolderType, CustodyRequestStatus, CustodyStatus,
    ShortfallDisposition, EffectiveRequestStatus,
)
# MARSOUD-ITEM-CUSTODY-01 (2026-08-07) — reuses CustodyHolderType
# from cash_custody. Item custody is the physical-things
# counterpart (laptops, uniforms, SIMs).
from app.models.item_custody import (
    CustodyItem, ItemCustodyRequest, ItemCustody,
    ItemCustodyRequestStatus, ItemCustodyStatus,
)
from app.models.department import Department
from app.models.attendance import (
    AttendancePolicy, PolicyScope, PolicyType,
)
from app.models.violation import (
    AttendanceViolationPolicy,
    LatePermissionRequest, PermissionStatus,
)
from app.models.checkin import AttendanceCheckin
from app.models.leave import (
    LeaveType, LeaveBalance,
    AttendanceException, AttendanceExceptionType,
    LeaveRequest, LeaveRequestStatus,
)
from app.models.crm import (
    Lead, LeadStatus, LeadType, LeadSource, LeadStatusEvent,
    Project, ProjectStatus, ProjectMember, Milestone, ProjectStatusEvent,
    PROJECT_TRANSITIONS,
    Task, TaskStatus, TaskPriority, KANBAN_ORDER,
    TaskComment, TaskActivityLog, task_assignees,
    LeadComment,
)
from app.models.task_schedule import (
    TaskSchedule, task_schedule_assignees,
    RECURRENCE_ONCE, RECURRENCE_DAILY, RECURRENCE_KINDS,
)
# MARSOUD-RECURRING-TASKS (Abdelhamid 2026-07-22).
from app.models.recurring_task import (
    RecurringTaskSeries, RecurringTaskException,
    FREQUENCIES, END_CONDITIONS,
    FREQ_DAILY, FREQ_WEEKLY, FREQ_MONTHLY, FREQ_YEARLY, FREQ_CUSTOM,
    END_NEVER, END_AFTER_N, END_ON_DATE,
)
# MARSOUD-QUOTAS (Abdelhamid 2026-07-22).
from app.models.quota import (
    Quota, AiTokenUsage, EmployeeAiCap, QuotaNotificationSent,
    QUOTA_USERS, QUOTA_AI_TOKENS_MONTH, QUOTA_STORAGE_BYTES, QUOTA_BRANCHES,
    KNOWN_QUOTA_TYPES,
    ENF_BLOCK, ENF_ALLOW_NOTIFY, ENF_UNLIMITED, ENFORCEMENT_MODES,
)
# MARSOUD-CRM-EXPANSION §2/5b/5c — new lightweight CRM tables.
from app.models.crm_expansion import (
    Campaign, LeadContact, LeadActivity, LeadActivityType,
)
from app.models.opsflow_extras import (
    Document, DocumentSourceType, DocumentVisibility,
    Notification, NotificationKind,
    ClientFeedback,
    AuditEntry,
)
from app.models.roles import Role, Permission, role_permissions
from app.models.inventory import (
    Warehouse, ProductVariant, StockBalance, StockMovement, StockMovementKind,
    StockTransfer, StockTransferItem, StockTransferStatus,
    StockLot, CashierShift, CashierShiftStatus,
)
from app.models.refund import (
    Refund, RefundType, CreditNote,
    VendorBillRefund, VendorRefundType, DebitNote,
)
from app.models.party_opening import PartyOpeningBalance, PartyType
from app.models.employee_reports import (
    EmployeeDailyReport, DailyReportStatus, EmployeeReportAccess,
)
from app.models.manufacturing import (
    BillOfMaterial, BOMLine, WorkOrder, WorkOrderStatus,
    WorkOrderConsumption,
)
from app.models.invitation import Invitation
from app.models.agent_chat import AgentMessage
from app.models.agent_conversation import AgentConversation
from app.models.agent_proposal import (
    AgentProposal, AgentDailyWriteCount,
    PROPOSAL_PENDING, PROPOSAL_EXECUTED,
    PROPOSAL_CANCELLED, PROPOSAL_EXPIRED,
    PROPOSAL_STATUSES,
)
from app.models.numbering import NumberSequence
from app.models.platform_audit import (
    PlatformAuditLog, SuperadminImpersonation, PlatformError,
    PlatformCronRun,
)
# MARSOUD-SIGNUP-AUTO-BLOCK (2026-08-12) — signup rejection
# log + auto-learned domain blocklist.
from app.models.signup_rejection import SignupRejection
from app.models.blocked_domain import BlockedDomain
# MARSOUD-BOT-REGISTRATION-VISIBILITY (2026-08-17) — per-email
# blocklist sibling of blocked_domains for TKT-17.
from app.models.blocked_email import BlockedEmail
# MARSOUD-APPROVAL-GATED-SUPERADMIN (2026-08-12) — pending
# queue for restricted-superadmin write attempts.
from app.models.pending_superadmin_action import (
    PendingSuperadminAction,
)
from app.models.api_token import ApiToken
# MARSOUD-MOBILE-TKT-05 (2026-08-18) — FCM registration tokens
# for push notifications on Flutter clients.
from app.models.push_token import PushToken
from app.models.activity import UserSession, UserActivityLog, ACTION_TYPES, SESSION_STATUS
from app.models.user_file import UserFile
from app.models.evaluation import (
    EvaluationCycle, EvaluationCyclePeriod, EvaluationCycleStatus,
    EvaluationCategory, ActualSource, BonusTier, AggregationMethod,
    EmployeeTarget, EmployeeMetricActual, EmployeeEvaluation,
    MetricLogEntry, EmployeeCategoryWeight, DEFAULT_CATEGORY_WEIGHTS,
)

__all__ = [
    "PlatformAuditLog", "SuperadminImpersonation", "PlatformError",
    "PlatformCronRun",
    "SignupRejection", "BlockedDomain", "BlockedEmail",
    "PendingSuperadminAction",
    "ApiToken", "PushToken",
    "UserFile",
    "EvaluationCycle", "EvaluationCyclePeriod", "EvaluationCycleStatus",
    "EvaluationCategory", "ActualSource", "BonusTier", "AggregationMethod",
    "EmployeeTarget", "EmployeeMetricActual", "EmployeeEvaluation",
    "MetricLogEntry", "EmployeeCategoryWeight", "DEFAULT_CATEGORY_WEIGHTS",
    "UserSession", "UserActivityLog", "ACTION_TYPES", "SESSION_STATUS",
    "User", "user_companies", "UserStatus", "Company",
    "Plan", "PlanPrice", "SubscriptionReminderSent",
    "DEFAULT_CURRENCY", "PlatformSetting",
    "ConsentEvent", "FeatureFlag", "CompanyFeatureOverride",
    "Coupon", "CouponRedemption",
    "DISCOUNT_PERCENT", "DISCOUNT_FIXED",
    "Broadcast",
    "AUDIENCE_ALL", "AUDIENCE_TRIAL", "AUDIENCE_ACTIVE",
    "AUDIENCE_EXPIRED", "AUDIENCE_BY_PLAN",
    "RecurringBill", "RecurringBillOverride",
    "INTERVAL_UNITS", "OVERRIDE_ACTIONS",
    "SalesCommission", "COMMISSION_STATUSES",
    "Account", "AccountType", "NormalSide",
    "JournalEntry", "JournalLine",
    "Invoice", "InvoiceItem", "InvoiceStatus", "Payment", "InvoiceReminderSent",
    "Customer", "Vendor", "CustomerComment", "CustomerNote",
    "FixedAsset", "DisposalReason",
    "Employee", "PayrollRun", "PayrollLine", "EmployeeAccrual", "Gender",
    "EmployeeHistory", "EmployeeChangeType",
    "AdvanceRequest", "AdvanceRequestStatus",
    "EmployeeAdvance", "AdvanceStatus", "AdvanceSource",
    "CashCustodyRequest", "CashCustody", "CashCustodySettlementLine",
    "CustodyHolderType", "CustodyRequestStatus", "CustodyStatus",
    "ShortfallDisposition",
    "CustodyItem", "ItemCustodyRequest", "ItemCustody",
    "ItemCustodyRequestStatus", "ItemCustodyStatus",
    "AdvanceRepayment",
    "OpenItem", "OpenItemSettlement", "OpenItemStatus",
    "SETTLEABLE_STATUSES",
    "Department",
    "LeaveType", "LeaveBalance",
    "AttendanceException", "AttendanceExceptionType",
    "AttendancePolicy", "PolicyScope", "PolicyType",
    "AttendanceCheckin",
    "LeaveRequest", "LeaveRequestStatus",
    "AttendanceViolationPolicy",
    "LatePermissionRequest", "PermissionStatus",
    "Lead", "LeadStatus", "LeadType", "LeadSource", "LeadStatusEvent",
    "Campaign", "LeadContact", "LeadActivity", "LeadActivityType",
    "Project", "ProjectStatus", "ProjectMember", "Milestone", "ProjectStatusEvent",
    "PROJECT_TRANSITIONS",
    "Task", "TaskStatus", "TaskPriority", "KANBAN_ORDER",
    "TaskComment", "TaskActivityLog", "task_assignees",
    "TaskSchedule", "task_schedule_assignees",
    "RecurringTaskSeries", "RecurringTaskException",
    "FREQUENCIES", "END_CONDITIONS",
    "FREQ_DAILY", "FREQ_WEEKLY", "FREQ_MONTHLY", "FREQ_YEARLY", "FREQ_CUSTOM",
    "END_NEVER", "END_AFTER_N", "END_ON_DATE",
    "Quota", "AiTokenUsage", "EmployeeAiCap", "QuotaNotificationSent",
    "QUOTA_USERS", "QUOTA_AI_TOKENS_MONTH",
    "QUOTA_STORAGE_BYTES", "QUOTA_BRANCHES", "KNOWN_QUOTA_TYPES",
    "ENF_BLOCK", "ENF_ALLOW_NOTIFY", "ENF_UNLIMITED", "ENFORCEMENT_MODES",
    "RECURRENCE_ONCE", "RECURRENCE_DAILY", "RECURRENCE_KINDS",
    "LeadComment",
    "Document", "DocumentSourceType", "DocumentVisibility",
    "Notification", "NotificationKind",
    "ClientFeedback", "AuditEntry",
    "Role", "Permission", "role_permissions",
    "Warehouse", "ProductVariant", "StockBalance",
    "StockMovement", "StockMovementKind",
    "StockTransfer", "StockTransferItem", "StockTransferStatus",
    "StockLot", "CashierShift", "CashierShiftStatus",
    "Refund", "RefundType", "CreditNote",
    "VendorBillRefund", "VendorRefundType", "DebitNote",
    "PartyOpeningBalance", "PartyType",
    "EmployeeDailyReport", "DailyReportStatus", "EmployeeReportAccess",
    "BillOfMaterial", "BOMLine",
    "WorkOrder", "WorkOrderStatus", "WorkOrderConsumption",
    "Invitation",
    "AgentMessage",
    "AgentConversation",
    "AgentProposal", "AgentDailyWriteCount",
    "PROPOSAL_PENDING", "PROPOSAL_EXECUTED",
    "PROPOSAL_CANCELLED", "PROPOSAL_EXPIRED",
    "PROPOSAL_STATUSES",
    "NumberSequence",
    "Product", "ProductGroup", "ProductCategory", "ProductUnit",
    "PaymentMethod",
    "DiscountType",
    "VendorBill", "VendorBillItem", "VendorBillPayment",
    "VendorBillStatus", "VendorBillPaymentMethod", "BillLineType",
    "VendorSubCategory",
    "DepreciationEntry",
]
