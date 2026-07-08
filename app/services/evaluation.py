"""MARSOUD-EVALUATIONS — score calculation + state transitions.

Public surface used by the routes:
  - create_cycle / update_cycle / delete_cycle
  - upsert_target / delete_target
  - upsert_actual / delete_actual
  - compute_score(cycle, employee_id)   → EmployeeEvaluation (persisted)
  - transition_status(cycle, new_status) → validated state hop
  - update_review(evaluation, notes, by_user_id)

Scoring formula (from Abdelhamid's spec 2026-07-08):

  For each target of an employee:
    achievement_pct = min(actual_value / target_value, 1.2) * 100
    → capped at 120% so one outlier can't drag the tier interpretation
      out of alignment with "EXCEEDED"

  Category score = weighted average of that category's metrics, using
  target.weight_pct as the weight. Missing actuals count as 0.

  Final blend:
    final = target_score  * 0.60
          + execution     * 0.25
          + growth        * 0.15

  Bonus tier from final_score:
    < 60          ZERO
    60 ≤ x < 80   PARTIAL
    80 ≤ x < 100  FULL
    ≥ 100         EXCEEDED
"""
from decimal import Decimal
from datetime import datetime

from app import db
from app.models import (
    EvaluationCycle, EvaluationCyclePeriod, EvaluationCycleStatus,
    EvaluationCategory, ActualSource, BonusTier,
    EmployeeTarget, EmployeeMetricActual, EmployeeEvaluation,
)


class EvaluationError(ValueError):
    """User-facing error the routes convert to a flash message."""


# ─── Cycle CRUD ─────────────────────────────────────────────────────────
def create_cycle(*, company_id, name, period_type, start_date,
                    end_date, created_by_id):
    if not (name or "").strip():
        raise EvaluationError("اسم الدورة مطلوب")
    if start_date > end_date:
        raise EvaluationError("تاريخ البداية يجب أن يسبق تاريخ النهاية")
    try:
        pt = EvaluationCyclePeriod(period_type)
    except (ValueError, TypeError):
        raise EvaluationError("نوع الدورة غير صالح")
    c = EvaluationCycle(
        company_id=company_id,
        name=name.strip(),
        period_type=pt.value,
        start_date=start_date,
        end_date=end_date,
        status=EvaluationCycleStatus.OPEN.value,
        created_by_id=created_by_id,
    )
    db.session.add(c)
    db.session.commit()
    return c


def delete_cycle(cycle):
    """Only permitted while OPEN — SUBMITTED/LOCKED cycles are frozen
    audit-quality records that must not disappear."""
    if cycle.status != EvaluationCycleStatus.OPEN.value:
        raise EvaluationError(
            "لا يمكن حذف دورة تم تقديمها أو قفلها — يجب فتحها مجدداً أولاً."
        )
    db.session.delete(cycle)
    db.session.commit()


def transition_status(cycle, new_status):
    """State machine: OPEN → SUBMITTED → LOCKED, plus admin reopen.

    The admin-reopen path (LOCKED → OPEN or SUBMITTED → OPEN) is
    exposed via a dedicated route with a stricter permission — the
    routes layer decides who's allowed to trigger it. This service
    validates the hop is a legal one; it does NOT gate on the caller.
    """
    try:
        target = EvaluationCycleStatus(new_status)
    except (ValueError, TypeError):
        raise EvaluationError("حالة الدورة غير صالحة")
    current = cycle.status
    legal = {
        "OPEN": {"SUBMITTED"},
        "SUBMITTED": {"LOCKED", "OPEN"},   # HR can reopen for corrections
        "LOCKED": {"OPEN"},                # owner-only in practice
    }
    if target.value not in legal.get(current, set()):
        raise EvaluationError(
            f"لا يمكن الانتقال من الحالة {current} إلى {target.value}"
        )
    cycle.status = target.value
    db.session.commit()
    return cycle


# ─── Target CRUD ────────────────────────────────────────────────────────
def upsert_target(*, cycle, employee_id, metric_key,
                     target_value, weight_pct, category, notes=None):
    """Insert or update the (cycle, employee, metric) row. The unique
    constraint at the DB layer catches the duplicate case too, but
    calling upsert lets the route pass the same form for both."""
    if not cycle.is_editable:
        raise EvaluationError(
            "لا يمكن تعديل الأهداف بعد قفل/تقديم الدورة"
        )
    key = (metric_key or "").strip()
    if not key:
        raise EvaluationError("مفتاح المتريك مطلوب")
    try:
        cat = EvaluationCategory(category)
    except (ValueError, TypeError):
        raise EvaluationError("فئة المتريك غير صالحة")
    try:
        tv = Decimal(str(target_value))
        wp = Decimal(str(weight_pct))
    except Exception:
        raise EvaluationError("قيمة أو وزن غير صحيح")
    if tv < 0 or wp < 0:
        raise EvaluationError("القيم يجب أن تكون غير سالبة")

    row = EmployeeTarget.query.filter_by(
        cycle_id=cycle.id, employee_id=employee_id, metric_key=key,
    ).first()
    if row is None:
        row = EmployeeTarget(
            cycle_id=cycle.id, employee_id=employee_id,
            metric_key=key,
        )
        db.session.add(row)
    row.target_value = tv
    row.weight_pct = wp
    row.category = cat.value
    row.notes = (notes or "").strip() or None
    db.session.commit()
    return row


def delete_target(target):
    if not target.cycle.is_editable:
        raise EvaluationError(
            "لا يمكن حذف الأهداف بعد قفل/تقديم الدورة"
        )
    db.session.delete(target)
    db.session.commit()


# ─── Actual CRUD ────────────────────────────────────────────────────────
def upsert_actual(*, cycle, employee_id, metric_key, actual_value,
                    source=ActualSource.MANUAL, entered_by_id=None,
                    evidence_note=None):
    if not cycle.is_editable:
        raise EvaluationError(
            "لا يمكن تعديل الأرقام الفعلية بعد قفل/تقديم الدورة"
        )
    key = (metric_key or "").strip()
    if not key:
        raise EvaluationError("مفتاح المتريك مطلوب")
    try:
        av = Decimal(str(actual_value))
    except Exception:
        raise EvaluationError("قيمة الفعلي غير صحيحة")
    try:
        src = ActualSource(
            source.value if hasattr(source, "value") else source
        )
    except (ValueError, TypeError):
        raise EvaluationError("مصدر الرقم غير صالح")

    row = EmployeeMetricActual.query.filter_by(
        cycle_id=cycle.id, employee_id=employee_id, metric_key=key,
    ).first()
    if row is None:
        row = EmployeeMetricActual(
            cycle_id=cycle.id, employee_id=employee_id, metric_key=key,
        )
        db.session.add(row)
    row.actual_value = av
    row.source = src.value
    row.entered_by_id = entered_by_id
    row.entered_at = datetime.utcnow()
    row.evidence_note = (evidence_note or "").strip() or None
    db.session.commit()
    return row


def delete_actual(actual):
    if not actual.cycle.is_editable:
        raise EvaluationError(
            "لا يمكن حذف الأرقام الفعلية بعد قفل/تقديم الدورة"
        )
    db.session.delete(actual)
    db.session.commit()


# ─── Score calculation (the heart) ─────────────────────────────────────
_ACHIEVEMENT_CAP = Decimal("120")   # min(actual/target, 1.2) * 100


def _achievement_pct(actual_value, target_value):
    """Percentage of target met, capped at 120%. Missing / zero target
    is treated as "no target" → return 0 so a metric without a
    weighted target can't game the score."""
    tv = Decimal(str(target_value or 0))
    av = Decimal(str(actual_value or 0))
    if tv <= 0:
        return Decimal("0")
    ratio = av / tv * Decimal("100")
    return min(ratio, _ACHIEVEMENT_CAP)


def _category_score(targets_in_cat, actuals_by_key):
    """Weighted average of per-metric achievement percentages, using
    weight_pct as the weight. Sum of weights doesn't have to be 100 —
    we divide by the actual sum so an incomplete target list still
    normalises correctly."""
    if not targets_in_cat:
        return Decimal("0")
    total_weight = Decimal("0")
    weighted_sum = Decimal("0")
    for t in targets_in_cat:
        w = Decimal(str(t.weight_pct or 0))
        if w <= 0:
            continue
        a = actuals_by_key.get(t.metric_key)
        actual_value = a.actual_value if a is not None else 0
        pct = _achievement_pct(actual_value, t.target_value)
        weighted_sum += pct * w
        total_weight += w
    if total_weight <= 0:
        return Decimal("0")
    return (weighted_sum / total_weight).quantize(Decimal("0.01"))


def _bonus_tier_for(final_score):
    fs = Decimal(str(final_score or 0))
    if fs < 60:
        return BonusTier.ZERO
    if fs < 80:
        return BonusTier.PARTIAL
    if fs < 100:
        return BonusTier.FULL
    return BonusTier.EXCEEDED


def compute_score(cycle, employee_id):
    """Read every target + actual for (cycle, employee_id), collapse
    to the three category scores + final_score + bonus_tier, and
    persist an EmployeeEvaluation row. Idempotent — re-running with
    updated actuals overwrites the previous row.

    Returns the persisted EmployeeEvaluation.
    """
    if cycle.status == EvaluationCycleStatus.LOCKED.value:
        raise EvaluationError(
            "لا يمكن إعادة حساب دورة مقفلة — افتحها أولاً لو محتاج تعديل."
        )

    targets = EmployeeTarget.query.filter_by(
        cycle_id=cycle.id, employee_id=employee_id,
    ).all()
    actuals = EmployeeMetricActual.query.filter_by(
        cycle_id=cycle.id, employee_id=employee_id,
    ).all()
    actuals_by_key = {a.metric_key: a for a in actuals}

    by_cat = {
        EvaluationCategory.TARGET_ACHIEVEMENT.value: [],
        EvaluationCategory.EXECUTION_QUALITY.value: [],
        EvaluationCategory.GROWTH.value: [],
    }
    for t in targets:
        by_cat.setdefault(t.category, []).append(t)

    target_score = _category_score(
        by_cat[EvaluationCategory.TARGET_ACHIEVEMENT.value],
        actuals_by_key,
    )
    execution_score = _category_score(
        by_cat[EvaluationCategory.EXECUTION_QUALITY.value],
        actuals_by_key,
    )
    growth_score = _category_score(
        by_cat[EvaluationCategory.GROWTH.value],
        actuals_by_key,
    )

    final_score = (
        target_score * Decimal("0.60")
        + execution_score * Decimal("0.25")
        + growth_score * Decimal("0.15")
    ).quantize(Decimal("0.01"))

    tier = _bonus_tier_for(final_score)

    row = EmployeeEvaluation.query.filter_by(
        cycle_id=cycle.id, employee_id=employee_id,
    ).first()
    if row is None:
        row = EmployeeEvaluation(
            cycle_id=cycle.id, employee_id=employee_id,
        )
        db.session.add(row)
    row.target_score = target_score
    row.execution_score = execution_score
    row.growth_score = growth_score
    row.final_score = final_score
    row.bonus_tier = tier.value
    db.session.commit()
    return row


def update_review(evaluation, notes, by_user_id):
    """Attach the reviewer's notes + sign-off timestamp to a computed
    evaluation. Allowed while the cycle is OPEN or SUBMITTED — LOCKED
    blocks it."""
    if evaluation.cycle.status == EvaluationCycleStatus.LOCKED.value:
        raise EvaluationError(
            "لا يمكن تعديل الملاحظات على دورة مقفلة"
        )
    evaluation.review_notes = (notes or "").strip() or None
    evaluation.reviewed_by_id = by_user_id
    evaluation.reviewed_at = datetime.utcnow()
    db.session.commit()
    return evaluation
