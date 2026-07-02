"""MARSOUD-MANUFACTURING-01 — BOM + work orders.

Four tables, all immutable-audit shaped like stock_movements:
  bill_of_materials     — one row per (finished-good variant, name)
  bom_lines             — one row per component with qty_per_unit
  work_orders           — MO-nnnn: DRAFT → IN_PROGRESS → COMPLETED
  work_order_consumption — one row per component actually consumed,
                            with unit_cost_at_time snapshot (mirrors
                            stock_movements' unit_cost_at_time)
"""
import enum
from datetime import datetime, date
from app import db


class WorkOrderStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"

    @property
    def label_ar(self):
        return {
            "DRAFT": "مسودة",
            "IN_PROGRESS": "جاري التنفيذ",
            "COMPLETED": "مكتمل",
            "CANCELLED": "ملغى",
        }[self.value]


class BillOfMaterial(db.Model):
    __tablename__ = "bill_of_materials"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                             nullable=False, index=True)
    product_variant_id = db.Column(db.Integer,
                                      db.ForeignKey("product_variants.id"),
                                      nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company")
    product_variant = db.relationship("ProductVariant")

    lines = db.relationship(
        "BOMLine", back_populates="bom",
        cascade="all, delete-orphan",
        order_by="BOMLine.id.asc()",
    )


class BOMLine(db.Model):
    __tablename__ = "bom_lines"
    id = db.Column(db.Integer, primary_key=True)
    bom_id = db.Column(db.Integer,
                          db.ForeignKey("bill_of_materials.id",
                                        ondelete="CASCADE"),
                          nullable=False, index=True)
    component_variant_id = db.Column(db.Integer,
                                        db.ForeignKey("product_variants.id"),
                                        nullable=False)
    qty_per_unit = db.Column(db.Numeric(15, 4), nullable=False)

    bom = db.relationship("BillOfMaterial", back_populates="lines")
    component_variant = db.relationship("ProductVariant")


class WorkOrder(db.Model):
    __tablename__ = "work_orders"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                             nullable=False, index=True)
    number = db.Column(db.String(30), nullable=False, index=True)
    bom_id = db.Column(db.Integer,
                         db.ForeignKey("bill_of_materials.id"),
                         nullable=False)
    warehouse_id = db.Column(db.Integer,
                               db.ForeignKey("warehouses.id"),
                               nullable=False)
    quantity_to_produce = db.Column(db.Numeric(15, 4), nullable=False)
    status = db.Column(db.Enum(WorkOrderStatus), nullable=False,
                          default=WorkOrderStatus.DRAFT, index=True)
    # Manually entered at completion time — the ticket rules out auto-
    # pulling from payroll to keep the first cut simple.
    direct_labor_cost = db.Column(db.Numeric(15, 4), default=0,
                                     nullable=False)
    overhead_cost = db.Column(db.Numeric(15, 4), default=0,
                                nullable=False)
    journal_entry_id = db.Column(db.Integer,
                                    db.ForeignKey("journal_entries.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                             nullable=False)

    company = db.relationship("Company")
    bom = db.relationship("BillOfMaterial")
    warehouse = db.relationship("Warehouse")
    journal_entry = db.relationship("JournalEntry",
                                       foreign_keys=[journal_entry_id])

    consumption = db.relationship(
        "WorkOrderConsumption", back_populates="work_order",
        cascade="all, delete-orphan",
    )


class WorkOrderConsumption(db.Model):
    """Immutable audit row — one per component actually consumed by a
    work order at completion. `unit_cost_at_time` snapshots the moving-
    average cost the material was drawn at, mirroring stock_movements
    exactly for reconciliation."""
    __tablename__ = "work_order_consumption"
    id = db.Column(db.Integer, primary_key=True)
    work_order_id = db.Column(db.Integer,
                                 db.ForeignKey("work_orders.id",
                                               ondelete="CASCADE"),
                                 nullable=False, index=True)
    component_variant_id = db.Column(db.Integer,
                                        db.ForeignKey("product_variants.id"),
                                        nullable=False)
    qty_consumed = db.Column(db.Numeric(15, 4), nullable=False)
    unit_cost_at_time = db.Column(db.Numeric(15, 4), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                             nullable=False)

    work_order = db.relationship("WorkOrder", back_populates="consumption")
    component_variant = db.relationship("ProductVariant")

    @property
    def total_cost(self):
        return float(self.qty_consumed or 0) * float(self.unit_cost_at_time or 0)
