"""MARSOUD-METRIC-LOG-REPORT (Abdelhamid 2026-07-14) — per-employee
report over MetricLogEntry rows.

Existing surface (`/evaluations/logs/`) shows only the LAST 30 raw
entries in the company, in insertion order. That's fine for a quick
data-entry audit but useless for the owner who wants to see "what
did we log for Rofida over the past month, per metric".

This service groups the raw entries into:
  · per (employee, cycle, metric_key) buckets
  · sum + count + min/max + latest value per bucket
  · optional filters: employee, cycle, metric_key, date range

The report route wraps this into a page that renders one card per
employee, with a small table per metric.
"""
from decimal import Decimal

from app import db
from app.models import (
    MetricLogEntry, Employee, EvaluationCycle,
)


def collect_per_employee(company_id, *, employee_id=None,
                             cycle_id=None, metric_key=None,
                             date_from=None, date_to=None):
    """Return a list of dicts, one per employee, each carrying a
    breakdown of every metric they've had logged in the current
    filter window.

    Shape:
      [
        {
          "employee": <Employee>,
          "metrics": [
            {"metric_key": str,
             "cycle_name": str,
             "count": int,
             "sum": float,
             "avg": float,
             "min": float,
             "max": float,
             "latest_value": float,
             "latest_date": date,
             "entries": [<MetricLogEntry>, ...],
            },
            ...
          ],
          "total_entries": int,
        },
        ...
      ]

    Empty list when nothing matches. Ordered by employee.name.
    """
    q = (
        MetricLogEntry.query
        .filter(MetricLogEntry.company_id == company_id)
        .join(Employee, MetricLogEntry.employee_id == Employee.id)
        .join(EvaluationCycle,
              MetricLogEntry.cycle_id == EvaluationCycle.id)
    )
    if employee_id:
        q = q.filter(MetricLogEntry.employee_id == employee_id)
    if cycle_id:
        q = q.filter(MetricLogEntry.cycle_id == cycle_id)
    if metric_key:
        q = q.filter(MetricLogEntry.metric_key == metric_key)
    if date_from:
        q = q.filter(MetricLogEntry.entry_date >= date_from)
    if date_to:
        q = q.filter(MetricLogEntry.entry_date <= date_to)

    rows = q.order_by(
        Employee.name.asc(),
        MetricLogEntry.metric_key.asc(),
        MetricLogEntry.entry_date.asc(),
    ).all()

    grouped = {}   # employee_id → {employee, metrics: {key: bucket}}
    for r in rows:
        emp_bucket = grouped.setdefault(r.employee_id, {
            "employee": r.employee,
            "metrics": {},
            "total_entries": 0,
        })
        key = (r.metric_key, r.cycle_id)
        m = emp_bucket["metrics"].get(key)
        if m is None:
            m = {
                "metric_key": r.metric_key,
                "cycle_name": (r.cycle.name if r.cycle
                                 else f"دورة #{r.cycle_id}"),
                "count": 0, "sum": 0.0,
                "min": float(r.value), "max": float(r.value),
                "latest_value": float(r.value),
                "latest_date": r.entry_date,
                "entries": [],
            }
            emp_bucket["metrics"][key] = m
        v = float(r.value or 0)
        m["count"] += 1
        m["sum"] += v
        if v < m["min"]:
            m["min"] = v
        if v > m["max"]:
            m["max"] = v
        # "Latest" = highest entry_date; tie-break on created_at.
        if (r.entry_date > m["latest_date"]
                or (r.entry_date == m["latest_date"]
                    and r.created_at
                    and r.entries_may_replace_latest(m))):
            m["latest_date"] = r.entry_date
            m["latest_value"] = v
        m["entries"].append(r)
        emp_bucket["total_entries"] += 1

    # Flatten per-employee metrics into a list + compute averages.
    out = []
    for emp_id, bucket in grouped.items():
        metric_list = []
        for key, m in bucket["metrics"].items():
            m["avg"] = (m["sum"] / m["count"]) if m["count"] else 0.0
            metric_list.append(m)
        metric_list.sort(key=lambda x: (x["cycle_name"], x["metric_key"]))
        out.append({
            "employee": bucket["employee"],
            "metrics": metric_list,
            "total_entries": bucket["total_entries"],
        })
    out.sort(key=lambda x: (x["employee"].name or "").lower())
    return out


# Method attached to MetricLogEntry-like duck to keep the
# latest-tiebreak logic pure — but simplest: monkey-patch a lightweight
# comparator into the class at import time so the sorting reads clean.
def _entries_may_replace_latest(self, m):
    prev = m["entries"][-1] if m["entries"] else None
    if not prev or prev.entry_date != self.entry_date:
        return True
    prev_created = prev.created_at
    if prev_created is None or self.created_at is None:
        return False
    return self.created_at > prev_created


MetricLogEntry.entries_may_replace_latest = _entries_may_replace_latest


def available_metric_keys(company_id):
    """Distinct metric_key values seen in the company's logs.
    Used to populate the report filter dropdown."""
    rows = (
        db.session.query(MetricLogEntry.metric_key)
        .filter(MetricLogEntry.company_id == company_id)
        .distinct()
        .order_by(MetricLogEntry.metric_key.asc())
        .all()
    )
    return [r[0] for r in rows if r[0]]
