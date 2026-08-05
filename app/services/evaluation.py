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
    EvaluationCategory, ActualSource, BonusTier, AggregationMethod,
    EmployeeTarget, EmployeeMetricActual, EmployeeEvaluation,
    MetricLogEntry, EmployeeCategoryWeight, DEFAULT_CATEGORY_WEIGHTS,
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
    # MARSOUD-EVAL-METRIC-LOG — when the cycle moves from OPEN to
    # SUBMITTED, roll up every metric-log into EmployeeMetricActual
    # automatically. Khadeeja never has to click a "compute" button;
    # the ticket's whole point is that once she stops logging, the
    # numbers are already there. Errors are logged, not raised —
    # a stale roll-up shouldn't block the transition itself.
    if current == "OPEN" and target == EvaluationCycleStatus.SUBMITTED:
        try:
            aggregate_actuals_for_cycle(cycle)
        except Exception:
            import logging
            logging.getLogger("marsoud.evaluation").exception(
                "auto-aggregate on SUBMITTED failed for cycle %s",
                cycle.id,
            )
    return cycle


# ─── Target CRUD ────────────────────────────────────────────────────────
def upsert_target(*, cycle, employee_id, metric_key,
                     target_value, weight_pct, category, notes=None,
                     aggregation_method=None):
    """Insert or update the (cycle, employee, metric) row. The unique
    constraint at the DB layer catches the duplicate case too, but
    calling upsert lets the route pass the same form for both.

    MARSOUD-EVAL-METRIC-LOG — `aggregation_method` determines how
    the raw MetricLogEntry rows collapse into the target's
    actual_value at cycle-close time. Default = SUM.
    """
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
    # Aggregation method: default to SUM if the caller didn't pass one.
    try:
        agg = (AggregationMethod(aggregation_method)
                if aggregation_method else AggregationMethod.SUM)
    except (ValueError, TypeError):
        raise EvaluationError("طريقة التجميع غير صالحة")

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
    row.aggregation_method = agg.value
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


def get_category_weights(cycle_id, employee_id):
    """MARSOUD-EVAL-CATEGORY-WEIGHT — resolve the three category
    weights for a (cycle, employee) tuple.

    Precedence:
      1. If any EmployeeCategoryWeight override exists for this
         (cycle, employee), use ALL THREE overrides (with 0 as the
         default for any category the user forgot to enter).
      2. Otherwise fall back to DEFAULT_CATEGORY_WEIGHTS (60/25/15).

    Returns a dict {TARGET_ACHIEVEMENT: 60, EXECUTION_QUALITY: 25,
    GROWTH: 15} — same shape as DEFAULT_CATEGORY_WEIGHTS, ready to
    plug into compute_score."""
    rows = EmployeeCategoryWeight.query.filter_by(
        cycle_id=cycle_id, employee_id=employee_id,
    ).all()
    if not rows:
        return dict(DEFAULT_CATEGORY_WEIGHTS)
    # Override mode: start with 0 for every category, fill in what
    # the user actually saved. This means a stored (0, 40, 60) row
    # set applies correctly — the missing category becomes 0, not
    # the default 60.
    out = {c.value: 0 for c in EvaluationCategory}
    for r in rows:
        out[r.category] = float(r.weight_pct or 0)
    return out


def set_category_weights(cycle, employee_id, weights):
    """Upsert all three category weight rows for a (cycle, employee).

    `weights` is a dict {TARGET_ACHIEVEMENT: 60, EXECUTION_QUALITY: 25,
    GROWTH: 15}. Enforces the sum-to-100 invariant so a garbled form
    submit can't produce a nonsensical blend downstream. Leaving out
    a category is treated as 0."""
    if not cycle.is_editable:
        raise EvaluationError(
            "لا يمكن تعديل الأوزان بعد قفل/تقديم الدورة"
        )
    # Normalise: every category must exist as a key (0 if omitted).
    normalised = {}
    for cat in EvaluationCategory:
        try:
            v = Decimal(str(weights.get(cat.value, 0)))
        except Exception:
            raise EvaluationError(
                f"وزن غير صحيح للفئة {cat.value}"
            )
        if v < 0:
            raise EvaluationError("الأوزان يجب أن تكون غير سالبة")
        normalised[cat.value] = v
    total = sum(normalised.values(), Decimal("0"))
    if abs(total - Decimal("100")) > Decimal("0.01"):
        raise EvaluationError(
            f"مجموع أوزان الفئات يجب أن يكون 100 (الحالي {total:g})"
        )
    # Upsert each row.
    for cat, w in normalised.items():
        row = EmployeeCategoryWeight.query.filter_by(
            cycle_id=cycle.id, employee_id=employee_id, category=cat,
        ).first()
        if row is None:
            row = EmployeeCategoryWeight(
                cycle_id=cycle.id, employee_id=employee_id,
                category=cat,
            )
            db.session.add(row)
        row.weight_pct = w
    db.session.commit()
    return normalised


def compute_score(cycle, employee_id):
    """Read every target + actual for (cycle, employee_id), collapse
    to the three category scores + final_score + bonus_tier, and
    persist an EmployeeEvaluation row. Idempotent — re-running with
    updated actuals overwrites the previous row.

    Category-level blend uses per-(cycle, employee) overrides when
    they exist (see get_category_weights). Otherwise the class-level
    60/25/15 defaults apply — so nothing changes for existing
    employees who never set custom weights.

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

    # MARSOUD-EVAL-CATEGORY-WEIGHT — pull the per-employee blend if
    # set, else fall back to the 60/25/15 defaults.
    weights = get_category_weights(cycle.id, employee_id)
    w_target = Decimal(str(weights[
        EvaluationCategory.TARGET_ACHIEVEMENT.value])) / Decimal("100")
    w_exec = Decimal(str(weights[
        EvaluationCategory.EXECUTION_QUALITY.value])) / Decimal("100")
    w_growth = Decimal(str(weights[
        EvaluationCategory.GROWTH.value])) / Decimal("100")

    final_score = (
        target_score * w_target
        + execution_score * w_exec
        + growth_score * w_growth
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


# ─── Raw metric-log entries + aggregation ─────────────────────────────
def targets_for(cycle, employee_id):
    """Return the list of EmployeeTarget rows for (cycle, employee).

    Used by the metric-log form's dependent dropdown so Khadeeja can
    only pick a metric_key that was actually agreed on for this
    (cycle, employee) — no free-form typos, no orphan logs."""
    return (EmployeeTarget.query
             .filter_by(cycle_id=cycle.id, employee_id=employee_id)
             .order_by(EmployeeTarget.metric_key)
             .all())


def log_metric_entry(*, company_id, cycle, employee_id, metric_key,
                        entry_date, value, entered_by_id,
                        source_activity_id=None):
    """Record one raw datapoint. Legal at any point while the cycle
    is not LOCKED — SUBMITTED cycles still accept logs so a manager
    can correct an entry before final sign-off. LOCKED is the only
    frozen state.

    Validates that a matching target exists for (cycle, employee,
    metric) so orphan entries can't accumulate.
    """
    if cycle.status == EvaluationCycleStatus.LOCKED.value:
        raise EvaluationError(
            "الدورة مقفلة — لا يمكن إضافة قيود جديدة."
        )
    key = (metric_key or "").strip()
    if not key:
        raise EvaluationError("المؤشر مطلوب")
    # Enforce dependent-dropdown contract at the service layer too.
    target = EmployeeTarget.query.filter_by(
        cycle_id=cycle.id, employee_id=employee_id, metric_key=key,
    ).first()
    if not target:
        raise EvaluationError(
            "المؤشر ده مش موجود كهدف للموظف في هذه الدورة — "
            "أضفه من صفحة الأهداف أولاً."
        )
    if entry_date is None:
        raise EvaluationError("التاريخ مطلوب")
    try:
        val = Decimal(str(value))
    except Exception:
        raise EvaluationError("القيمة غير صحيحة")

    row = MetricLogEntry(
        company_id=company_id,
        cycle_id=cycle.id,
        employee_id=employee_id,
        metric_key=key,
        entry_date=entry_date,
        value=val,
        entered_by_id=entered_by_id,
        # MARSOUD-METRIC-AUTOMATION — set by the cron job, NULL for the
        # manual path, which is unchanged.
        source_activity_id=source_activity_id,
    )
    db.session.add(row)
    db.session.commit()
    return row


def delete_log_entry(entry):
    if entry.cycle.status == EvaluationCycleStatus.LOCKED.value:
        raise EvaluationError(
            "الدورة مقفلة — لا يمكن حذف قيود منها."
        )
    db.session.delete(entry)
    db.session.commit()


def _collapse(entries, method):
    """Reduce a list of MetricLogEntry to a single Decimal per the
    target's aggregation method. Empty input → 0."""
    if not entries:
        return Decimal("0")
    vals = [Decimal(str(e.value or 0)) for e in entries]
    if method == AggregationMethod.SUM.value:
        return sum(vals, Decimal("0"))
    if method == AggregationMethod.AVERAGE.value:
        total = sum(vals, Decimal("0"))
        return (total / Decimal(len(vals))).quantize(Decimal("0.0001"))
    if method == AggregationMethod.LATEST.value:
        # Take the entry with the max entry_date; ties broken by created_at.
        latest = max(
            entries,
            key=lambda e: (e.entry_date, e.created_at or e.entry_date),
        )
        return Decimal(str(latest.value or 0))
    # Unknown method → fall back to SUM (defensive).
    return sum(vals, Decimal("0"))


def aggregate_actuals_for_cycle(cycle):
    """MARSOUD-EVAL-METRIC-LOG — for every EmployeeTarget in the
    cycle, look up the raw log entries for that (employee, metric),
    collapse them per the target's aggregation_method, and upsert
    the result into EmployeeMetricActual with source=AUTO_AGGREGATED.

    Idempotent: re-running overwrites the same actual rows.

    Skips targets with zero log entries — leaves any manual actual
    already present alone (so a mixed workflow where SOME metrics
    are entered manually and OTHERS aggregated still works).
    """
    from datetime import datetime as _dt
    touched = 0
    for target in EmployeeTarget.query.filter_by(cycle_id=cycle.id).all():
        logs = MetricLogEntry.query.filter_by(
            cycle_id=cycle.id,
            employee_id=target.employee_id,
            metric_key=target.metric_key,
        ).all()
        if not logs:
            continue   # leave any existing manual actual alone
        rolled = _collapse(logs, target.aggregation_method)

        row = EmployeeMetricActual.query.filter_by(
            cycle_id=cycle.id,
            employee_id=target.employee_id,
            metric_key=target.metric_key,
        ).first()
        if row is None:
            row = EmployeeMetricActual(
                cycle_id=cycle.id,
                employee_id=target.employee_id,
                metric_key=target.metric_key,
            )
            db.session.add(row)
        row.actual_value = rolled
        row.source = ActualSource.AUTO_AGGREGATED.value
        row.entered_at = _dt.utcnow()
        row.evidence_note = (
            f"جُمعت من {len(logs)} قيد "
            f"({target.aggregation_method})"
        )
        touched += 1
    db.session.commit()
    return touched


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
