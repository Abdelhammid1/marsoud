"""MARSOUD-EVALUATIONS — monthly employee-performance evaluation layer.

Four models sitting on top of the operational tables (Lead / Task /
Project / EmployeeDailyReport / SalesCommission). They convert raw
activity into a monthly score (0-100) + bonus tier without duplicating
any of the source data — targets + actuals are entered per cycle, and
the compute_score service (services/evaluation.py) collapses them into
a final EmployeeEvaluation row.

Design constraints from Abdelhamid's spec (2026-07-08):
  · metric_key is FREE-FORM VARCHAR — adding a metric later must NOT
    require a code change + deploy. Enums are the wrong tool here.
  · Every table is company-scoped for multi-tenant isolation.
  · Status is a manual state machine (see EvaluationCycleStatus).
"""
import enum
from datetime import datetime
from app import db


class EvaluationCyclePeriod(str, enum.Enum):
    TRIAL = "TRIAL"
    MONTHLY = "MONTHLY"


class EvaluationCycleStatus(str, enum.Enum):
    OPEN = "OPEN"           # data being gathered
    SUBMITTED = "SUBMITTED" # HR sealed for owner review
    LOCKED = "LOCKED"       # owner signed off; no further edits


class EvaluationCategory(str, enum.Enum):
    """Which pillar of the 60/25/15 final-score blend a target
    contributes to. See services/evaluation.py::compute_score."""
    TARGET_ACHIEVEMENT = "TARGET_ACHIEVEMENT"
    EXECUTION_QUALITY = "EXECUTION_QUALITY"
    GROWTH = "GROWTH"


class ActualSource(str, enum.Enum):
    MANUAL = "MANUAL"                 # human entered — v1 default
    AUTO = "AUTO"                     # scraped from operational tables (future)
    AUTO_AGGREGATED = "AUTO_AGGREGATED"  # rolled up from MetricLogEntry


class AggregationMethod(str, enum.Enum):
    """MARSOUD-EVAL-METRIC-LOG — how the raw log entries for one
    (employee, metric) are collapsed into a single actual_value
    at cycle-close time. Set per EmployeeTarget so different
    metrics under the same cycle can use different math."""
    AVERAGE = "AVERAGE"   # mean of every logged value
    SUM = "SUM"           # sum of every logged value
    LATEST = "LATEST"     # value of the most recent entry_date


class BonusTier(str, enum.Enum):
    ZERO = "ZERO"           # < 60
    PARTIAL = "PARTIAL"     # 60 <= x < 80
    FULL = "FULL"           # 80 <= x < 100
    EXCEEDED = "EXCEEDED"   # >= 100


class EvaluationCycle(db.Model):
    __tablename__ = "evaluation_cycles"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                             db.ForeignKey("companies.id"),
                             nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    period_type = db.Column(db.String(20), nullable=False,
                              default=EvaluationCyclePeriod.MONTHLY.value)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(20), nullable=False,
                         default=EvaluationCycleStatus.OPEN.value)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                                nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                             nullable=False)

    company = db.relationship("Company")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    targets = db.relationship(
        "EmployeeTarget", backref="cycle",
        cascade="all, delete-orphan", lazy="dynamic",
    )
    actuals = db.relationship(
        "EmployeeMetricActual", backref="cycle",
        cascade="all, delete-orphan", lazy="dynamic",
    )
    evaluations = db.relationship(
        "EmployeeEvaluation", backref="cycle",
        cascade="all, delete-orphan", lazy="dynamic",
    )

    @property
    def is_editable(self):
        """OPEN cycles accept target/actual edits; anything else is
        frozen from the user's POV. LOCKED cycles are frozen even
        against admins."""
        return self.status == EvaluationCycleStatus.OPEN.value

    @property
    def status_label_ar(self):
        return {
            "OPEN": "مفتوحة",
            "SUBMITTED": "مقدَّمة للمراجعة",
            "LOCKED": "مقفلة",
        }.get(self.status, self.status)


class EmployeeTarget(db.Model):
    __tablename__ = "employee_targets"

    id = db.Column(db.Integer, primary_key=True)
    cycle_id = db.Column(
        db.Integer,
        db.ForeignKey("evaluation_cycles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    employee_id = db.Column(
        db.Integer,
        db.ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    metric_key = db.Column(db.String(120), nullable=False)
    target_value = db.Column(db.Numeric(15, 4), nullable=False, default=0)
    weight_pct = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    category = db.Column(db.String(30), nullable=False)
    notes = db.Column(db.Text, nullable=True)
    # MARSOUD-EVAL-METRIC-LOG — how the raw log entries for this
    # (employee, metric) collapse into a single actual_value at
    # cycle-close time. Default SUM matches the most common intuition
    # ("total done in the period"); AVERAGE and LATEST cover weekly
    # rates and single-datapoint indicators respectively.
    aggregation_method = db.Column(
        db.String(20), nullable=False,
        default=AggregationMethod.SUM.value,
    )

    employee = db.relationship("Employee")

    __table_args__ = (
        db.UniqueConstraint(
            "cycle_id", "employee_id", "metric_key",
            name="uq_target_cycle_employee_metric",
        ),
    )


class EmployeeMetricActual(db.Model):
    __tablename__ = "employee_metric_actuals"

    id = db.Column(db.Integer, primary_key=True)
    cycle_id = db.Column(
        db.Integer,
        db.ForeignKey("evaluation_cycles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    employee_id = db.Column(
        db.Integer,
        db.ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    metric_key = db.Column(db.String(120), nullable=False)
    actual_value = db.Column(db.Numeric(15, 4), nullable=False, default=0)
    source = db.Column(db.String(20), nullable=False,
                         default=ActualSource.MANUAL.value)
    entered_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                                nullable=True)
    entered_at = db.Column(db.DateTime, default=datetime.utcnow,
                             nullable=False)
    evidence_note = db.Column(db.Text, nullable=True)

    employee = db.relationship("Employee")
    entered_by = db.relationship("User", foreign_keys=[entered_by_id])

    __table_args__ = (
        db.UniqueConstraint(
            "cycle_id", "employee_id", "metric_key",
            name="uq_actual_cycle_employee_metric",
        ),
    )


class EmployeeEvaluation(db.Model):
    __tablename__ = "employee_evaluations"

    id = db.Column(db.Integer, primary_key=True)
    cycle_id = db.Column(
        db.Integer,
        db.ForeignKey("evaluation_cycles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    employee_id = db.Column(
        db.Integer,
        db.ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    target_score = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    execution_score = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    growth_score = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    final_score = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    bonus_tier = db.Column(db.String(20), nullable=False,
                             default=BonusTier.ZERO.value)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                                 nullable=True)
    review_notes = db.Column(db.Text, nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    employee = db.relationship("Employee")
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_id])

    __table_args__ = (
        db.UniqueConstraint(
            "cycle_id", "employee_id",
            name="uq_evaluation_cycle_employee",
        ),
    )

    @property
    def bonus_tier_label_ar(self):
        return {
            "ZERO": "لا بونص",
            "PARTIAL": "بونص جزئي",
            "FULL": "بونص كامل",
            "EXCEEDED": "متجاوز الأهداف",
        }.get(self.bonus_tier, self.bonus_tier)

    @property
    def bonus_tier_badge_class(self):
        return {
            "ZERO": "badge-cancelled",
            "PARTIAL": "badge-partial",
            "FULL": "badge-paid",
            "EXCEEDED": "badge-paid",
        }.get(self.bonus_tier, "badge")


class EmployeeCategoryWeight(db.Model):
    """MARSOUD-EVAL-CATEGORY-WEIGHT — per-(cycle, employee, category)
    override for the final-score blend. When no row exists for a
    (cycle, employee, category) triple, compute_score falls back to
    the class-level defaults DEFAULT_CATEGORY_WEIGHTS. When rows do
    exist, they must sum to 100% across the three categories —
    validated at the service layer.
    """
    __tablename__ = "employee_category_weights"

    id = db.Column(db.Integer, primary_key=True)
    cycle_id = db.Column(
        db.Integer,
        db.ForeignKey("evaluation_cycles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    employee_id = db.Column(
        db.Integer,
        db.ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    category = db.Column(db.String(30), nullable=False)
    weight_pct = db.Column(db.Numeric(6, 2), nullable=False, default=0)

    cycle = db.relationship("EvaluationCycle")
    employee = db.relationship("Employee")

    __table_args__ = (
        db.UniqueConstraint(
            "cycle_id", "employee_id", "category",
            name="uq_ecw_cycle_employee_category",
        ),
    )


# The blend the spec called out — used when an employee has NO
# per-category override rows for the cycle. Sum = 100.
DEFAULT_CATEGORY_WEIGHTS = {
    EvaluationCategory.TARGET_ACHIEVEMENT.value: 60,
    EvaluationCategory.EXECUTION_QUALITY.value: 25,
    EvaluationCategory.GROWTH.value: 15,
}


class MetricLogEntry(db.Model):
    """MARSOUD-EVAL-METRIC-LOG — one raw datapoint Khadeeja logged
    for (cycle, employee, metric) on a specific date. The
    aggregate_actuals service collapses every log for a (metric,
    employee) into one EmployeeMetricActual row at cycle-close
    time using the target's aggregation_method (SUM / AVERAGE /
    LATEST).

    Design constraints from Abdelhamid's spec (2026-07-09):
      · No week_number, no bucketing — just a free-form date.
      · Multiple entries per date + metric are legal (the aggregator
        handles them). The UI form is deliberately minimal — pick
        employee/cycle/metric/date/value + save.
    """
    __tablename__ = "metric_log_entries"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                             db.ForeignKey("companies.id"),
                             nullable=False, index=True)
    cycle_id = db.Column(
        db.Integer,
        db.ForeignKey("evaluation_cycles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    employee_id = db.Column(
        db.Integer,
        db.ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    metric_key = db.Column(db.String(120), nullable=False)
    entry_date = db.Column(db.Date, nullable=False)
    value = db.Column(db.Numeric(15, 4), nullable=False, default=0)
    entered_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                                nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                             nullable=False)

    company = db.relationship("Company")
    cycle = db.relationship(
        "EvaluationCycle",
        backref=db.backref("metric_logs",
                             cascade="all, delete-orphan",
                             lazy="dynamic"),
    )
    employee = db.relationship("Employee")
    entered_by = db.relationship("User", foreign_keys=[entered_by_id])
