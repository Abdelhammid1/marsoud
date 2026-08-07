"""MARSOUD-INSIGHTS-AGENT-PROFESSIONAL (2026-08-06) — CRM reads
(leads, projects, campaigns, customer extras).

The accountant already exposes list_customers + party_statement.
This module adds the CRM-side reads that the accountant never had:
lead pipeline, project detail, campaigns, per-customer deposits,
sales commissions.
"""
from __future__ import annotations
from datetime import date, timedelta
from app import db
from app.agent.insights_catalog import (
    register, has_perm, perm_denied, parse_date,
)


# ─── crm_list_leads ─────────────────────────────────────────────
@register(
    name="crm_list_leads",
    description=(
        "قائمة العملاء المحتملين (leads) مع فلاتر: status، "
        "assigned_to، campaign، بحث نصي، ومدى تواريخ الإنشاء."),
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "assigned_to_id": {"type": "integer"},
            "campaign_id": {"type": "integer"},
            "search": {"type": "string"},
            "date_from": {"type": "string"},
            "date_to": {"type": "string"},
            "limit": {"type": "integer"},
        },
    },
    permission="leads.view",
)
def crm_list_leads(args, company_id, user_id):
    if not has_perm(user_id, company_id, "leads.view"):
        return perm_denied("leads.view")
    from app.models import Lead, LeadStatus
    from sqlalchemy import or_
    q = Lead.query.filter(
        Lead.company_id == company_id, Lead.deleted_at.is_(None))
    if args.get("status"):
        try:
            q = q.filter(Lead.status == LeadStatus[args["status"]])
        except KeyError:
            return {"error": f"status غير معروف: {args['status']!r}"}
    if args.get("assigned_to_id"):
        q = q.filter(Lead.assigned_to_id == int(args["assigned_to_id"]))
    if args.get("campaign_id"):
        q = q.filter(Lead.campaign_id == int(args["campaign_id"]))
    if args.get("search"):
        s = args["search"]
        q = q.filter(or_(Lead.client_name.ilike(f"%{s}%"),
                         Lead.email.ilike(f"%{s}%"),
                         Lead.phone.ilike(f"%{s}%")))
    if args.get("date_from"):
        q = q.filter(Lead.created_at >= parse_date(args["date_from"]))
    if args.get("date_to"):
        q = q.filter(Lead.created_at <= parse_date(args["date_to"]))
    limit = min(int(args.get("limit") or 50), 200)
    rows = q.order_by(Lead.created_at.desc()).limit(limit).all()
    return {
        "count": q.count(),
        "leads": [
            {"id": l.id,
             "client_name": l.client_name,
             "service_needed": l.service_needed,
             "status": l.status.value if l.status else None,
             "assigned_to_id": l.assigned_to_id,
             "campaign_id": l.campaign_id,
             "expected_value": float(l.expected_value or 0),
             "created_at": (l.created_at.isoformat()
                            if l.created_at else None),
             "next_meeting": (l.next_meeting.isoformat()
                              if l.next_meeting else None)}
            for l in rows
        ],
    }


# ─── crm_get_lead ──────────────────────────────────────────────
@register(
    name="crm_get_lead",
    description=(
        "تفاصيل عميل محتمل بالـ id مع سجل تغيير الحالات والأنشطة."),
    input_schema={
        "type": "object",
        "properties": {"lead_id": {"type": "integer"}},
        "required": ["lead_id"],
    },
    permission="leads.view",
)
def crm_get_lead(args, company_id, user_id):
    if not has_perm(user_id, company_id, "leads.view"):
        return perm_denied("leads.view")
    from app.models import Lead, LeadStatusEvent, LeadActivity
    lead = Lead.query.filter_by(
        id=int(args["lead_id"]),
        company_id=company_id).first()
    if not lead or lead.deleted_at:
        return {"error": "lead غير موجود"}
    history = (LeadStatusEvent.query
               .filter_by(lead_id=lead.id)
               .order_by(LeadStatusEvent.created_at.asc()).all())
    activities = (LeadActivity.query
                  .filter_by(lead_id=lead.id)
                  .order_by(LeadActivity.created_at.desc())
                  .limit(20).all()
                  if has_perm(user_id, company_id, "crm.activities.view")
                  else [])
    return {
        "lead": {
            "id": lead.id, "client_name": lead.client_name,
            "email": lead.email, "phone": lead.phone,
            "service_needed": lead.service_needed,
            "status": lead.status.value if lead.status else None,
            "assigned_to_id": lead.assigned_to_id,
            "campaign_id": lead.campaign_id,
            "expected_value": float(lead.expected_value or 0),
            "created_at": (lead.created_at.isoformat()
                           if lead.created_at else None),
            "converted_at": (lead.converted_at.isoformat()
                             if lead.converted_at else None),
            "notes": lead.notes,
            "request_description": lead.request_description,
            "sales_action_required": lead.sales_action_required,
        },
        "status_history": [
            {"from_status": (h.old_status.value
                             if getattr(h, "old_status", None) else None),
             "to_status": (h.new_status.value
                           if getattr(h, "new_status", None) else None),
             "created_at": (h.created_at.isoformat()
                            if h.created_at else None)}
            for h in history
        ],
        "activities": [
            {"id": a.id,
             "kind": (a.kind.value
                      if getattr(a.kind, "value", None) else str(a.kind)),
             "note": getattr(a, "note", None),
             "created_at": (a.created_at.isoformat()
                            if a.created_at else None)}
            for a in activities
        ],
    }


# ─── crm_pipeline_counts ───────────────────────────────────────
@register(
    name="crm_pipeline_counts",
    description=(
        "توزيع العملاء المحتملين على مراحل الـ pipeline: كم "
        "في كل حالة + إجمالي القيمة المتوقعة (expected value)."),
    input_schema={"type": "object", "properties": {}},
    permission="leads.view",
)
def crm_pipeline_counts(args, company_id, user_id):
    if not has_perm(user_id, company_id, "leads.view"):
        return perm_denied("leads.view")
    from app.models import Lead, LeadStatus
    out = {}
    for s in LeadStatus:
        rows = Lead.query.filter(
            Lead.company_id == company_id,
            Lead.deleted_at.is_(None),
            Lead.status == s).all()
        out[s.value] = {
            "count": len(rows),
            "expected_value_sum": float(
                sum((r.expected_value or 0) for r in rows)),
        }
    return {"by_status": out,
            "total_open": sum(v["count"] for k, v in out.items()
                              if k not in ("WON", "LOST", "NO_RESPONSE"))}


# ─── crm_list_projects ─────────────────────────────────────────
@register(
    name="crm_list_projects",
    description=(
        "قائمة المشاريع مع الفلاتر: status، manager، بحث في "
        "الاسم."),
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "manager_id": {"type": "integer"},
            "search": {"type": "string"},
            "limit": {"type": "integer"},
        },
    },
    permission="projects.view",
)
def crm_list_projects(args, company_id, user_id):
    if not has_perm(user_id, company_id, "projects.view"):
        return perm_denied("projects.view")
    from app.models import Project, ProjectStatus
    q = Project.query.filter_by(
        company_id=company_id).filter(Project.deleted_at.is_(None))
    if args.get("status"):
        try:
            q = q.filter(Project.status == ProjectStatus[args["status"]])
        except KeyError:
            return {"error": f"status غير معروف: {args['status']!r}"}
    if args.get("manager_id"):
        q = q.filter(Project.manager_id == int(args["manager_id"]))
    if args.get("search"):
        q = q.filter(Project.name.ilike(f"%{args['search']}%"))
    limit = min(int(args.get("limit") or 50), 200)
    rows = q.order_by(Project.created_at.desc()).limit(limit).all()
    return {
        "count": q.count(),
        "projects": [
            {"id": p.id, "name": p.name,
             "status": p.status.value if p.status else None,
             "customer_id": p.customer_id,
             "manager_id": p.manager_id,
             "start_date": (p.start_date.isoformat()
                            if p.start_date else None),
             "end_date": (p.end_date.isoformat()
                          if p.end_date else None),
             "progress_pct": float(p.progress_pct or 0)}
            for p in rows
        ],
    }


# ─── crm_get_project ───────────────────────────────────────────
@register(
    name="crm_get_project",
    description="تفاصيل مشروع + الأعضاء + المراحل (milestones).",
    input_schema={
        "type": "object",
        "properties": {"project_id": {"type": "integer"}},
        "required": ["project_id"],
    },
    permission="projects.view",
)
def crm_get_project(args, company_id, user_id):
    if not has_perm(user_id, company_id, "projects.view"):
        return perm_denied("projects.view")
    from app.models import Project, ProjectMember, Milestone
    p = Project.query.filter_by(
        id=int(args["project_id"]),
        company_id=company_id).first()
    if not p:
        return {"error": "project غير موجود"}
    members = ProjectMember.query.filter_by(project_id=p.id).all()
    ms = Milestone.query.filter_by(project_id=p.id).order_by(
        Milestone.order.asc()).all()
    return {
        "project": {
            "id": p.id, "name": p.name,
            "status": p.status.value if p.status else None,
            "customer_id": p.customer_id,
            "manager_id": p.manager_id,
            "start_date": p.start_date.isoformat() if p.start_date else None,
            "end_date": p.end_date.isoformat() if p.end_date else None,
            "progress_pct": float(p.progress_pct or 0),
            "notes": p.notes,
        },
        "members": [{"user_id": m.user_id,
                     "added_at": (m.added_at.isoformat()
                                  if m.added_at else None)}
                    for m in members],
        "milestones": [{"id": m.id, "name": m.name,
                        "order": m.order,
                        "target_date": (m.target_date.isoformat()
                                        if m.target_date else None),
                        "completed_at": (m.completed_at.isoformat()
                                         if m.completed_at else None)}
                       for m in ms],
    }


# ─── crm_customer_deposits ─────────────────────────────────────
@register(
    name="crm_customer_deposits",
    description=(
        "الدفعات المقدّمة (deposits) النشطة لعميل واحد مع "
        "الإجمالي."),
    input_schema={
        "type": "object",
        "properties": {"customer_id": {"type": "integer"}},
        "required": ["customer_id"],
    },
    permission="customers.view",
)
def crm_customer_deposits(args, company_id, user_id):
    if not has_perm(user_id, company_id, "customers.view"):
        return perm_denied("customers.view")
    try:
        from app.services.deposits import (
            active_deposits_for_customer, total_active_amount,
        )
    except ImportError:
        return {"error": "deposits service غير متاحة"}
    cid = int(args["customer_id"])
    deps = active_deposits_for_customer(cid)
    return {
        "customer_id": cid,
        "active_deposits": [
            {"id": d.id, "amount": float(d.amount or 0),
             "created_at": (d.created_at.isoformat()
                            if getattr(d, "created_at", None) else None)}
            for d in deps
        ],
        "total_active_amount": float(total_active_amount(cid) or 0),
    }
