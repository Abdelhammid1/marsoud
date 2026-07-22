from app.models.user import User, user_companies, UserStatus
from app.models.company import Company
from app.models.plan import Plan, SubscriptionReminderSent
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
from app.models.partner import Customer, Vendor
from app.models.asset import FixedAsset, DepreciationEntry
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
from app.models.department import Department
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
from app.models.numbering import NumberSequence
from app.models.platform_audit import PlatformAuditLog, SuperadminImpersonation, PlatformError
from app.models.api_token import ApiToken
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
    "ApiToken",
    "UserFile",
    "EvaluationCycle", "EvaluationCyclePeriod", "EvaluationCycleStatus",
    "EvaluationCategory", "ActualSource", "BonusTier", "AggregationMethod",
    "EmployeeTarget", "EmployeeMetricActual", "EmployeeEvaluation",
    "MetricLogEntry", "EmployeeCategoryWeight", "DEFAULT_CATEGORY_WEIGHTS",
    "UserSession", "UserActivityLog", "ACTION_TYPES", "SESSION_STATUS",
    "User", "user_companies", "UserStatus", "Company",
    "Plan", "SubscriptionReminderSent", "PlatformSetting",
    "RecurringBill", "RecurringBillOverride",
    "INTERVAL_UNITS", "OVERRIDE_ACTIONS",
    "SalesCommission", "COMMISSION_STATUSES",
    "Account", "AccountType", "NormalSide",
    "JournalEntry", "JournalLine",
    "Invoice", "InvoiceItem", "InvoiceStatus", "Payment", "InvoiceReminderSent",
    "Customer", "Vendor",
    "FixedAsset",
    "Employee", "PayrollRun", "PayrollLine", "EmployeeAccrual", "Gender",
    "EmployeeHistory", "EmployeeChangeType",
    "Department",
    "LeaveType", "LeaveBalance",
    "AttendanceException", "AttendanceExceptionType",
    "LeaveRequest", "LeaveRequestStatus",
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
    "NumberSequence",
    "Product", "ProductGroup", "ProductCategory", "ProductUnit",
    "PaymentMethod",
    "DiscountType",
    "VendorBill", "VendorBillItem", "VendorBillPayment",
    "VendorBillStatus", "VendorBillPaymentMethod", "BillLineType",
    "VendorSubCategory",
    "DepreciationEntry",
]
