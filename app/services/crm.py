"""CRM + Projects + Tasks services — business logic for Cycle 7.

The route layer stays thin; this module owns:
  - lead status transitions with audit-log events
  - lead → won → convert to project (auto-creates Marsoud Customer)
  - project status transitions with gated check + audit events
  - task lifecycle hooks that recompute project progress
"""
from datetime import datetime, date
from app import db
from app.models import (
    Lead, LeadStatus, LeadStatusEvent,
    Project, ProjectStatus, ProjectStatusEvent, PROJECT_TRANSITIONS,
    Task, TaskStatus,
    Customer,
)


class CRMError(Exception):
    """Domain error raised by the CRM/Projects/Tasks services."""


# ─── Leads ───────────────────────────────────────────────────────────────
def change_lead_status(lead, new_status, *, changed_by_id, note=None,
                       lost_reason=None):
    """Transition a lead to a new status + record an event row."""
    if not isinstance(new_status, LeadStatus):
        try:
            new_status = LeadStatus[new_status]
        except KeyError:
            raise CRMError("حالة غير معروفة")
    if lead.is_converted:
        raise CRMError("لا يمكن تغيير حالة عميل تم تحويله لمشروع")
    if new_status == lead.status:
        raise CRMError("الحالة الحالية مطابقة")

    old = lead.status
    lead.status = new_status
    if new_status == LeadStatus.LOST:
        lead.lost_reason = (lost_reason or "").strip() or None

    db.session.add(LeadStatusEvent(
        lead_id=lead.id, from_status=old, to_status=new_status,
        changed_by_id=changed_by_id, note=(note or "").strip() or None,
    ))
    db.session.commit()
    return lead


def convert_lead_to_project(lead, *, project_name, project_type, manager_id,
                             start_date, end_date, created_by_id,
                             customer_id=None):
    """Lead → Won → "Convert to Project".

    Auto-creates a Marsoud `Customer` from lead.client_name/email/phone if
    one doesn't already match by email. Returns the new Project.
    """
    if lead.status != LeadStatus.WON:
        raise CRMError("لا يمكن التحويل قبل الوصول لحالة (ربحناها)")
    if lead.is_converted:
        raise CRMError("هذا العميل المحتمل تم تحويله مسبقاً")
    if end_date <= start_date:
        raise CRMError("تاريخ النهاية يجب أن يكون بعد البداية")

    # Resolve customer — either explicit or auto from the lead.
    customer = None
    if customer_id:
        customer = db.session.get(Customer, customer_id)
        if not customer or customer.company_id != lead.company_id:
            raise CRMError("العميل المختار غير موجود")
    else:
        # Try to dedupe by email within the same company.
        if lead.email:
            customer = Customer.query.filter_by(
                company_id=lead.company_id, email=lead.email.lower(),
            ).first()
        if customer is None:
            customer = Customer(
                company_id=lead.company_id,
                name=lead.client_name,
                email=(lead.email or "").lower() or None,
                phone=lead.phone,
            )
            db.session.add(customer)
            db.session.flush()

    project = Project(
        company_id=lead.company_id,
        name=project_name.strip(),
        lead_id=lead.id,
        customer_id=customer.id,
        type=project_type.strip(),
        manager_id=manager_id,
        start_date=start_date,
        end_date=end_date,
        status=ProjectStatus.PLANNING,
    )
    db.session.add(project)
    db.session.flush()

    db.session.add(ProjectStatusEvent(
        project_id=project.id, from_status=None,
        to_status=ProjectStatus.PLANNING,
        changed_by_id=created_by_id,
        note=f"أُنشئ من تحويل العميل المحتمل #{lead.id}",
    ))
    lead.converted_at = datetime.utcnow()
    lead.converted_customer_id = customer.id
    db.session.commit()
    return project


# ─── Projects ────────────────────────────────────────────────────────────
def change_project_status(project, new_status, *, changed_by_id, note=None):
    if not isinstance(new_status, ProjectStatus):
        try:
            new_status = ProjectStatus[new_status]
        except KeyError:
            raise CRMError("حالة غير معروفة")
    if not project.can_transition_to(new_status):
        allowed = ", ".join(s.label_ar for s in PROJECT_TRANSITIONS.get(project.status, []))
        raise CRMError(f"الانتقال غير مسموح. المسموح حالياً: {allowed or '— لا يوجد —'}")

    old = project.status
    project.status = new_status
    db.session.add(ProjectStatusEvent(
        project_id=project.id, from_status=old, to_status=new_status,
        changed_by_id=changed_by_id, note=(note or "").strip() or None,
    ))
    db.session.commit()
    return project


# ─── Tasks ───────────────────────────────────────────────────────────────
def set_task_status(task, new_status, *, by_user_id=None):
    """Move a task to a new status, update completed_at, and recompute
    parent project progress."""
    if not isinstance(new_status, TaskStatus):
        try:
            new_status = TaskStatus[new_status]
        except KeyError:
            raise CRMError("حالة مهمة غير معروفة")

    old = task.status
    task.status = new_status
    if new_status == TaskStatus.DONE:
        task.completed_at = datetime.utcnow()
    elif old == TaskStatus.DONE and new_status != TaskStatus.DONE:
        # Reverting from done — clear timestamp so progress recomputes correctly.
        task.completed_at = None

    project = task.project
    if project:
        project.recompute_progress()
    db.session.commit()
    return task
