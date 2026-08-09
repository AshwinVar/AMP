import os
from datetime import datetime
from sqlalchemy import (Boolean, Column, Integer, String, ForeignKey, DateTime, Date,
                        Text, Float, UniqueConstraint)
from sqlalchemy import true as sa_true
from sqlalchemy.orm import relationship

from database import Base

# Trial length for new companies (days from tenant creation).
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "30"))


class Machine(Base):
    """A physical machine, identified by (tenant, site, name).

    IDENTITY IS A TRIPLE, NOT A NAME. `name` used to be the whole identity for
    ingest, and it is not unique — two factories both run a "CNC-01". MQTT
    resolved by name alone, so a packet from one customer's gateway landed on
    whichever row the database returned first. Measured before the fix: one
    factory's CNC-01 was flipped to Breakdown by the other factory's telemetry.

    `site` is the middle term because a single customer legitimately runs the
    same machine name at two plants. It is NOT NULL with an empty-string default
    rather than nullable: in PostgreSQL NULL != NULL, so a UNIQUE constraint over
    a nullable column does not actually prevent duplicates.
    """

    __tablename__ = "machines"
    __table_args__ = (
        UniqueConstraint("tenant_code", "site", "name", name="uq_machine_identity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    site = Column(String, nullable=False, default="", server_default="")
    name = Column(String, nullable=False)
    status = Column(String, nullable=False)
    utilization = Column(Integer, default=0)
    downtime = Column(String, default="0 min")
    line = Column(String, default="")                          # production line, e.g. "SMT" | "IC"

    downtime_logs = relationship("DowntimeLog", back_populates="machine")
    production_records = relationship("ProductionRecord", back_populates="machine")


class DowntimeLog(Base):
    __tablename__ = "downtime_logs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    machine_id = Column(Integer, ForeignKey("machines.id"))
    reason = Column(String, nullable=False)
    duration = Column(String, nullable=False)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    machine = relationship("Machine", back_populates="downtime_logs")


class ShiftData(Base):
    __tablename__ = "shift_data"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    shift_name = Column(String, nullable=False)
    target_output = Column(Integer, nullable=False)
    actual_output = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ProductionRecord(Base):
    __tablename__ = "production_records"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    machine_id = Column(Integer, ForeignKey("machines.id"))
    planned_minutes = Column(Integer, nullable=False)
    runtime_minutes = Column(Integer, nullable=False)
    ideal_cycle_time_seconds = Column(Integer, nullable=False)
    total_count = Column(Integer, nullable=False)
    good_count = Column(Integer, nullable=False)
    rejected_count = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    machine = relationship("Machine", back_populates="production_records")


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    alert_type = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    password = Column(String, nullable=False)
    role = Column(String, nullable=False)
    tenant_code = Column(String, default="DEFAULT", nullable=True)
    # Whether this login may still act. There was no way to revoke access at all:
    # auth.get_current_user decodes the JWT and performs no database lookup, so a
    # DELETED user's token kept working until it expired on its own. Measured —
    # a user removed from the database still approved a purchase order.
    #
    # Deliberately NOT enforced on every request (that would add a SELECT to each
    # one). It is enforced at the boundaries where it matters: approving or
    # rejecting an agent action, which moves money and material.
    is_active = Column(Boolean, nullable=False, default=True, server_default=sa_true())


class MachineEvent(Base):
    __tablename__ = "machine_events"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    machine_id = Column(Integer, ForeignKey("machines.id"))
    machine_name = Column(String, nullable=False)
    old_status = Column(String, nullable=True)
    new_status = Column(String, nullable=False)
    utilization = Column(Integer, default=0)
    source = Column(String, default="mqtt")
    created_at = Column(DateTime, default=datetime.utcnow)


class WorkOrder(Base):
    __tablename__ = "work_orders"
    __table_args__ = (
        UniqueConstraint("tenant_code", "work_order_no", name="uq_work_orders_tenant_work_order_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    work_order_no = Column(String, nullable=False)
    part_number = Column(String, nullable=False)
    batch_number = Column(String, nullable=False)
    machine_id = Column(Integer, ForeignKey("machines.id"))
    target_quantity = Column(Integer, nullable=False)
    actual_quantity = Column(Integer, default=0)
    status = Column(String, default="Planned")
    material_state = Column(String, default="RAW")             # RAW -> SEMI (post-SMT) -> FIN (post-IC)
    planned_start = Column(DateTime, nullable=True)
    planned_end = Column(DateTime, nullable=True)
    # Stamped ONCE, the first time the order reaches Completed. This is what makes
    # the ProductionCompleted publish idempotent: `status` alone cannot say whether
    # the BOM has already moved, because a finished order can be reopened and
    # finished again. Also the only real completion timestamp any table carries.
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProductionPlan(Base):
    __tablename__ = "production_plans"
    __table_args__ = (
        UniqueConstraint("tenant_code", "plan_no", name="uq_production_plans_tenant_plan_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    plan_no = Column(String, nullable=False)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"))
    machine_id = Column(Integer, ForeignKey("machines.id"))
    planned_quantity = Column(Integer, nullable=False)
    actual_quantity = Column(Integer, default=0)
    plan_date = Column(Date, nullable=False)
    shift_name = Column(String, nullable=False)
    status = Column(String, default="Planned")
    created_at = Column(DateTime, default=datetime.utcnow)



class Escalation(Base):
    __tablename__ = "escalations"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    machine_id = Column(Integer, ForeignKey("machines.id"), nullable=True)
    title = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    owner = Column(String, nullable=False)
    department = Column(String, nullable=False)
    status = Column(String, default="Open")
    source = Column(String, default="Manual")
    notes = Column(String, nullable=True)
    resolution_notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)



class InventoryItem(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (
        UniqueConstraint("tenant_code", "item_code", name="uq_inventory_items_tenant_item_code"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    item_code = Column(String, nullable=False)
    item_name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    supplier = Column(String, nullable=True)
    unit = Column(String, nullable=False)
    current_stock = Column(Integer, default=0)
    reorder_level = Column(Integer, default=0)
    location = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class InventoryTransaction(Base):
    __tablename__ = "inventory_transactions"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    item_id = Column(Integer, ForeignKey("inventory_items.id"))
    transaction_type = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    reference = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class EventLog(Base):
    """Append-only log of domain events — the factory's history and the
    substrate for analytics, AI and the digital twin (ADR-0001)."""
    __tablename__ = "event_log"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    event_type = Column(String, index=True, nullable=False)
    event_version = Column(Integer, default=1)
    payload = Column(Text)
    occurred_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class QualityInspection(Base):
    __tablename__ = "quality_inspections"
    __table_args__ = (
        UniqueConstraint("tenant_code", "inspection_no", name="uq_quality_inspections_tenant_inspection_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    inspection_no = Column(String, nullable=False)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    production_plan_id = Column(Integer, ForeignKey("production_plans.id"), nullable=True)
    machine_id = Column(Integer, ForeignKey("machines.id"), nullable=True)
    inspector = Column(String, nullable=False)
    inspected_quantity = Column(Integer, nullable=False)
    passed_quantity = Column(Integer, default=0)
    failed_quantity = Column(Integer, default=0)
    defect_category = Column(String, nullable=True)
    rework_quantity = Column(Integer, default=0)
    scrap_quantity = Column(Integer, default=0)
    status = Column(String, default="Open")
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)



class FactoryLayoutNode(Base):
    __tablename__ = "factory_layout_nodes"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    machine_id = Column(Integer, ForeignKey("machines.id"), nullable=True)
    node_name = Column(String, nullable=False)
    node_type = Column(String, default="Machine")
    x_position = Column(Integer, default=50)
    y_position = Column(Integer, default=50)
    width = Column(Integer, default=160)
    height = Column(Integer, default=100)
    zone = Column(String, default="Production")
    created_at = Column(DateTime, default=datetime.utcnow)



class CustomerOrder(Base):
    __tablename__ = "customer_orders"
    __table_args__ = (
        UniqueConstraint("tenant_code", "order_no", name="uq_customer_orders_tenant_order_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    order_no = Column(String, nullable=False)
    customer_name = Column(String, nullable=False)
    product_name = Column(String, nullable=False)
    linked_work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    linked_production_plan_id = Column(Integer, ForeignKey("production_plans.id"), nullable=True)
    order_quantity = Column(Integer, nullable=False)
    dispatched_quantity = Column(Integer, default=0)
    priority = Column(String, default="Medium")
    due_date = Column(Date, nullable=False)
    status = Column(String, default="Pending")
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)



class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint("tenant_code", "supplier_code", name="uq_suppliers_tenant_supplier_code"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    supplier_code = Column(String, nullable=False)
    supplier_name = Column(String, nullable=False)
    contact_person = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    category = Column(String, nullable=True)
    status = Column(String, default="Active")
    created_at = Column(DateTime, default=datetime.utcnow)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint("tenant_code", "po_no", name="uq_purchase_orders_tenant_po_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    po_no = Column(String, nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    item_id = Column(Integer, ForeignKey("inventory_items.id"), nullable=True)
    item_name = Column(String, nullable=False)
    order_quantity = Column(Integer, nullable=False)
    received_quantity = Column(Integer, default=0)
    unit = Column(String, nullable=False)
    expected_delivery_date = Column(Date, nullable=False)
    status = Column(String, default="Open")
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)



class ComplianceDocument(Base):
    __tablename__ = "compliance_documents"
    __table_args__ = (
        UniqueConstraint("tenant_code", "document_no", name="uq_compliance_documents_tenant_document_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    document_no = Column(String, nullable=False)
    title = Column(String, nullable=False)
    document_type = Column(String, nullable=False)
    department = Column(String, nullable=False)
    version = Column(String, default="1.0")
    owner = Column(String, nullable=False)
    approval_status = Column(String, default="Draft")
    review_due_date = Column(Date, nullable=False)
    storage_link = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MaintenanceTask(Base):
    __tablename__ = "maintenance_tasks"
    __table_args__ = (
        UniqueConstraint("tenant_code", "task_no", name="uq_maintenance_tasks_tenant_task_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    task_no = Column(String, nullable=False)
    machine_id = Column(Integer, ForeignKey("machines.id"))
    task_type = Column(String, nullable=False)
    priority = Column(String, default="Medium")
    assigned_to = Column(String, nullable=False)
    planned_date = Column(Date, nullable=False)
    completed_date = Column(Date, nullable=True)
    downtime_minutes = Column(Integer, default=0)
    spare_parts_used = Column(String, nullable=True)
    status = Column(String, default="Open")
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProductionSchedule(Base):
    __tablename__ = "production_schedules"
    __table_args__ = (
        UniqueConstraint("tenant_code", "schedule_no", name="uq_production_schedules_tenant_schedule_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    schedule_no = Column(String, nullable=False)
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    production_plan_id = Column(Integer, ForeignKey("production_plans.id"), nullable=True)
    machine_id = Column(Integer, ForeignKey("machines.id"))
    shift_name = Column(String, nullable=False)
    scheduled_date = Column(Date, nullable=False)
    priority = Column(String, default="Medium")
    planned_quantity = Column(Integer, nullable=False)
    estimated_minutes = Column(Integer, default=480)
    status = Column(String, default="Scheduled")
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)



class IoTTelemetry(Base):
    __tablename__ = "iot_telemetry"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    machine_id = Column(Integer, ForeignKey("machines.id"))
    signal_name = Column(String, nullable=False)
    signal_value = Column(String, nullable=False)
    numeric_value = Column(Integer, default=0)
    unit = Column(String, nullable=True)
    source = Column(String, default="MQTT")
    created_at = Column(DateTime, default=datetime.utcnow)


class AIRecommendation(Base):
    __tablename__ = "ai_recommendations"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    recommendation_type = Column(String, nullable=False)
    severity = Column(String, default="Medium")
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)
    related_machine_id = Column(Integer, ForeignKey("machines.id"), nullable=True)
    confidence = Column(Integer, default=75)
    status = Column(String, default="Open")
    created_at = Column(DateTime, default=datetime.utcnow)


class AgentAction(Base):
    """The record of an autonomous agent action (ADR-0005): the audit log and the
    approval queue in one. Agents propose (status Proposed); a human approves or
    rejects, advancing or cancelling the underlying item (a task or a PO)."""
    __tablename__ = "agent_actions"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    agent = Column(String, nullable=False)             # "maintenance" | "reorder"
    action_type = Column(String, nullable=False)       # "open_task" | "draft_po"
    summary = Column(String, nullable=False)
    ref_kind = Column(String, nullable=False)          # "maintenance_task" | "purchase_order"
    ref_id = Column(Integer, nullable=True)
    severity = Column(String, default="Medium")
    # Proposed | Approved | Rejected | Expired | Cancelled
    #
    # Expired and Cancelled are new. Before them the only way an action left the
    # queue was a human decision, so a proposal made a year ago was still
    # approvable — measured: a 400-day-old draft PO was approved and advanced.
    # A recommendation is evidence about a moment; acting on it much later is
    # acting on a fact that has probably stopped being true.
    status = Column(String, default="Proposed")
    related_machine_id = Column(Integer, nullable=True)
    decided_by = Column(String, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    # After this instant the proposal may no longer be acted on. Nullable so a
    # row written before this column existed (or by a path that declines to set
    # it) is treated as "no explicit expiry" and falls back to the age limit in
    # approvals.py — a NULL must not mean "never expires".
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)



class TenantConfig(Base):
    """Per-tenant licensing, feature flags and white-label branding.
    Keyed by tenant_code (the same identity used across users and GMATS inventory).
    One row drives: which module packs a company can see (licensing/feature flags),
    its branding (white-label), and its subscription/trial state (billing)."""
    __tablename__ = "tenant_configs"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, unique=True, index=True, nullable=False)
    plan = Column(String, default="enterprise")                 # starter / growth / enterprise / demo
    enabled_modules = Column(String, default="core,operations,factory,intelligence,admin")  # CSV of module keys
    brand_name = Column(String, default="AMP")
    brand_color = Column(String, default="#6366f1")
    brand_logo_url = Column(String, nullable=True)
    subscription_status = Column(String, default="trial")       # trial / active / past_due / cancelled
    trial_ends_at = Column(DateTime, nullable=True)
    # £ of margin (or contribution) per good unit — set per tenant so the recovery
    # read-model can value the OEE gap in money. NULL = unset (report units only,
    # never a made-up figure).
    unit_value_gbp = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AgentPolicy(Base):
    """Per-tenant agent-autonomy policy (ADR-0004/0005): which agents may act
    without human approval. No row for a tenant -> fall back to the
    AUTO_APPROVE_AGENTS env default. Stored as a CSV of agent keys; "" is a real
    choice ("no agent auto-approves"), distinct from having no row. A new table,
    so create_all provisions it everywhere (no ALTER needed)."""
    __tablename__ = "agent_policies"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, unique=True, index=True, nullable=False)
    auto_approve_agents = Column(String, default="")            # CSV of agent keys
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BillOfMaterials(Base):
    """One tenant's recipe for one finished product, at one revision.

    REPLACES bom.PART_BOM, a module-level dict shared by every customer.
    Measured before this table existed, with two customers who had never heard
    of each other and both happening to make a part they call SHAFT-001:

        FACTORY_B completes a work order for 10 of THEIR OWN SHAFT-001:
           FACTORY_B  RM-STEEL-001   stock=80     (was 100)

    FACTORY_B's stock moved at 2 per unit - GMATS's rate, a number nobody at
    FACTORY_B entered and could not change. That is not a data leak (isolation
    held; GMATS's own stock was untouched) but a correctness failure: if their
    shaft really consumes 3.5kg, their inventory is quietly wrong forever.

    The other half is worse. A product NOT in the dict moved nothing at all:

        FACTORY_B completes 50 of VALVE-77 - a product THEY make:
           B-ALLOY-9      stock=500    (unchanged)
           inventory transactions written: 0

    A customer's own products consumed no material and produced no finished
    goods, silently. So the platform only worked for the one company whose
    recipes were compiled into it.

    HEADER PLUS LINES, not one flat table. A bill of materials has many
    components, and the finished good produced is a property of the PRODUCT, not
    of each component. Flattening it would let two lines of one BOM disagree
    about what they build - a contradiction the schema should not be able to
    represent.
    """
    __tablename__ = "bills_of_materials"
    __table_args__ = (
        UniqueConstraint("tenant_code", "part_number", "revision",
                         name="uq_bom_tenant_part_revision"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    part_number = Column(String, nullable=False)      # what a work order names
    revision = Column(String, nullable=False, default="A")
    # The InventoryItem received when a work order for part_number completes.
    # Nullable because not every part yields a stocked finished good - the old
    # dict had `"fg": None` for two of its six parts, and that is a real case
    # (a sub-operation that only consumes).
    output_item_code = Column(String, nullable=True)
    effective_from = Column(Date, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    notes = Column(Text, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    components = relationship("BomComponent", back_populates="bom",
                              cascade="all, delete-orphan")


class BomComponent(Base):
    """One component line: how much of one item goes into one finished product."""
    __tablename__ = "bom_components"
    __table_args__ = (
        UniqueConstraint("bom_id", "component_code", name="uq_bom_component"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # tenant_code is carried on the line as well as the header. Redundant by
    # construction, and deliberately so: every tenant-owned table in this schema
    # carries it, the ADR-0002 scoping hook filters on it, and a line reachable
    # only through its header would be invisible to that hook.
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    bom_id = Column(Integer, ForeignKey("bills_of_materials.id"), nullable=False,
                    index=True)
    component_code = Column(String, nullable=False)   # an InventoryItem.item_code
    # Float, not Integer. The old dict's consume_per_unit was an int, which
    # cannot express "1.5 kg of bar per shaft" - the single most ordinary thing
    # a bill of materials says.
    quantity_per_unit = Column(Float, nullable=False, default=0.0)
    unit = Column(String, nullable=False, default="")

    bom = relationship("BillOfMaterials", back_populates="components")


class DocumentSequence(Base):
    """The next number to issue for one (tenant, document type).

    Replaces `count() + 1`, which is a population rather than a sequence: it
    reused numbers after a deletion and handed the same number to two concurrent
    requests. See doc_numbers.py for why a reused document number is a
    reconciliation problem even when nothing crashes.

    A new table, so create_all provisions it everywhere and the first allocation
    per tenant seeds itself from the numbers already issued — no data migration.
    """
    __tablename__ = "document_sequences"
    __table_args__ = (
        UniqueConstraint("tenant_code", "doc_type",
                         name="uq_document_sequences_tenant_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False)
    doc_type = Column(String, nullable=False)        # MIS / GRN / CC / RMN ...
    next_value = Column(Integer, nullable=False, default=1)


class CompanyTenant(Base):
    __tablename__ = "company_tenants"

    id = Column(Integer, primary_key=True, index=True)
    company_code = Column(String, unique=True, nullable=False)
    company_name = Column(String, nullable=False)
    industry = Column(String, nullable=True)
    plan_name = Column(String, default="Starter")
    subscription_status = Column(String, default="Trial")
    seats = Column(Integer, default=5)
    monthly_fee = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    @property
    def trial_days_left(self):
        """Days of trial remaining (0 = expired today), or None off-trial."""
        if self.subscription_status != "Trial" or not self.created_at:
            return None
        return max(0, TRIAL_DAYS - (datetime.utcnow() - self.created_at).days)

    @property
    def trial_expired(self):
        """True when a Trial tenant has outlived TRIAL_DAYS."""
        return (self.subscription_status == "Trial" and self.created_at is not None
                and (datetime.utcnow() - self.created_at).days >= TRIAL_DAYS)


class CostRecord(Base):
    __tablename__ = "cost_records"
    __table_args__ = (
        UniqueConstraint("tenant_code", "cost_no", name="uq_cost_records_tenant_cost_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    cost_no = Column(String, nullable=False)
    cost_type = Column(String, nullable=False)
    reference_type = Column(String, nullable=True)
    reference_id = Column(Integer, nullable=True)
    description = Column(String, nullable=False)
    amount = Column(Integer, default=0)
    department = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class OperatorJobExecution(Base):
    __tablename__ = "operator_job_executions"
    __table_args__ = (
        UniqueConstraint("tenant_code", "execution_no", name="uq_operator_job_executions_tenant_execution_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    execution_no = Column(String, nullable=False)
    operator_name = Column(String, nullable=False)
    machine_id = Column(Integer, ForeignKey("machines.id"))
    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True)
    production_plan_id = Column(Integer, ForeignKey("production_plans.id"), nullable=True)
    job_status = Column(String, default="Started")
    good_count = Column(Integer, default=0)
    rejected_count = Column(Integer, default=0)
    notes = Column(String, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)



class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    # ADR-0002 tenant scoping. NULLABLE with NO default on purpose: request writes
    # are auto-stamped (tenancy.before_flush); a row created without a tenant
    # context (system audit) stays NULL and is hidden from every tenant's read
    # (with_loader_criteria(tenant_code == tenant) never matches NULL) — fail-safe,
    # never silently assigned to DEFAULT.
    tenant_code = Column(String, index=True, nullable=True)
    actor = Column(String, default="system")
    action = Column(String, nullable=False)
    entity_type = Column(String, nullable=True)
    entity_id = Column(Integer, nullable=True)
    details = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    notification_type = Column(String, nullable=False)
    severity = Column(String, default="Info")
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)
    status = Column(String, default="Unread")
    created_at = Column(DateTime, default=datetime.utcnow)


class ReportRequest(Base):
    __tablename__ = "report_requests"
    __table_args__ = (
        UniqueConstraint("tenant_code", "report_no", name="uq_report_requests_tenant_report_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    report_no = Column(String, nullable=False)
    report_type = Column(String, nullable=False)
    requested_by = Column(String, default="Admin")
    format = Column(String, default="PDF")
    status = Column(String, default="Generated")
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

# ─────────────────────────────────────────────────────────────────
# GMATS — Tenant-scoped enterprise inventory (4-bucket stock model)
# Reusable multi-tenant inventory rig. tenant_code isolates each client.
# ─────────────────────────────────────────────────────────────────

class GmatsItem(Base):
    __tablename__ = "gmats_items"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, default="GMATS", index=True)
    item_code = Column(String, nullable=False)
    item_name = Column(String, nullable=False)
    category = Column(String, default="General")
    unit = Column(String, default="Nos")
    physical_stock = Column(Integer, default=0)     # what's actually on the rack
    reserved_stock = Column(Integer, default=0)     # blocked by open proformas
    reorder_level = Column(Integer, default=0)      # min stock → purchase alert
    location = Column(String, nullable=True)
    purchase_rate = Column(Integer, default=0)
    supplier = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # available = physical_stock - reserved_stock (computed in API)


class GmatsAlias(Base):
    __tablename__ = "gmats_aliases"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, default="GMATS", index=True)
    item_id = Column(Integer, ForeignKey("gmats_items.id"))
    alias_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class GmatsProforma(Base):
    __tablename__ = "gmats_proformas"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, default="GMATS", index=True)
    proforma_no = Column(String, nullable=False)
    customer_name = Column(String, nullable=False)
    status = Column(String, default="Open")          # Open / Invoiced / Cancelled
    created_at = Column(DateTime, default=datetime.utcnow)


class GmatsProformaLine(Base):
    __tablename__ = "gmats_proforma_lines"

    id = Column(Integer, primary_key=True, index=True)
    proforma_id = Column(Integer, ForeignKey("gmats_proformas.id"))
    item_id = Column(Integer, ForeignKey("gmats_items.id"))
    qty = Column(Integer, nullable=False)


class GmatsInvoice(Base):
    __tablename__ = "gmats_invoices"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, default="GMATS", index=True)
    invoice_no = Column(String, nullable=False)
    proforma_id = Column(Integer, ForeignKey("gmats_proformas.id"), nullable=True)
    customer_name = Column(String, nullable=False)
    status = Column(String, default="Generated")     # Generated
    created_at = Column(DateTime, default=datetime.utcnow)


class GmatsMIN(Base):
    """Material Issue Note — free spares supplied with a machine (not billed)."""
    __tablename__ = "gmats_min"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, default="GMATS", index=True)
    min_no = Column(String, nullable=False)
    customer_name = Column(String, nullable=False)
    machine_ref = Column(String, nullable=True)      # e.g. "20 HP Screw Compressor"
    status = Column(String, default="Issued")
    created_at = Column(DateTime, default=datetime.utcnow)


class GmatsMINLine(Base):
    __tablename__ = "gmats_min_lines"

    id = Column(Integer, primary_key=True, index=True)
    min_id = Column(Integer, ForeignKey("gmats_min.id"))
    item_id = Column(Integer, ForeignKey("gmats_items.id"))
    qty = Column(Integer, nullable=False)


class Remnant(Base):
    __tablename__ = "remnants"
    __table_args__ = (
        UniqueConstraint("tenant_code", "tag_no", name="uq_remnants_tenant_tag_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=True)  # ADR-0002 fail-safe (see AuditLog)
    tag_no = Column(String, nullable=False)
    item_id = Column(Integer, ForeignKey("inventory_items.id"))
    source_reference = Column(String, nullable=True)   # WO or PO that generated this remnant
    original_qty = Column(Integer, nullable=False)
    remaining_qty = Column(Integer, nullable=False)
    unit = Column(String, nullable=False)
    location = Column(String, nullable=True)
    status = Column(String, default="Available")       # Available / In Use / Consumed / Scrapped
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class MaterialIssueSlip(Base):
    __tablename__ = "material_issue_slips"
    __table_args__ = (
        UniqueConstraint("tenant_code", "slip_no", name="uq_material_issue_slips_tenant_slip_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=True)  # ADR-0002 fail-safe (see AuditLog)
    slip_no = Column(String, nullable=False)
    item_id = Column(Integer, ForeignKey("inventory_items.id"))
    remnant_id = Column(Integer, ForeignKey("remnants.id"), nullable=True)
    work_order_ref = Column(String, nullable=True)
    requested_qty = Column(Integer, nullable=False)
    issued_qty = Column(Integer, default=0)
    requested_by = Column(String, nullable=False)
    approved_by = Column(String, nullable=True)
    status = Column(String, default="Pending")         # Pending / Approved / Issued / Rejected
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    issued_at = Column(DateTime, nullable=True)


class GoodsReceiptNote(Base):
    __tablename__ = "goods_receipt_notes"
    __table_args__ = (
        UniqueConstraint("tenant_code", "grn_no", name="uq_goods_receipt_notes_tenant_grn_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=True)  # ADR-0002 fail-safe (see AuditLog)
    grn_no = Column(String, nullable=False)
    purchase_order_ref = Column(String, nullable=True)
    supplier_name = Column(String, nullable=False)
    received_by = Column(String, nullable=False)
    status = Column(String, default="Draft")           # Draft / Accepted / Partial
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class GRNItem(Base):
    __tablename__ = "grn_items"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=True)  # ADR-0002 fail-safe (see AuditLog)
    grn_id = Column(Integer, ForeignKey("goods_receipt_notes.id"))
    item_id = Column(Integer, ForeignKey("inventory_items.id"))
    lot_no = Column(String, nullable=True)
    ordered_qty = Column(Integer, default=0)
    received_qty = Column(Integer, nullable=False)
    accepted_qty = Column(Integer, nullable=False)
    rejected_qty = Column(Integer, default=0)
    inspection_status = Column(String, default="Accepted")  # Accepted / Rejected / Partial
    created_at = Column(DateTime, default=datetime.utcnow)


class CycleCount(Base):
    __tablename__ = "cycle_counts"
    __table_args__ = (
        UniqueConstraint("tenant_code", "count_no", name="uq_cycle_counts_tenant_count_no"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=True)  # ADR-0002 fail-safe (see AuditLog)
    count_no = Column(String, nullable=False)
    counted_by = Column(String, nullable=False)
    status = Column(String, default="Draft")           # Draft / Submitted / Approved
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CycleCountItem(Base):
    __tablename__ = "cycle_count_items"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=True)  # ADR-0002 fail-safe (see AuditLog)
    count_id = Column(Integer, ForeignKey("cycle_counts.id"))
    item_id = Column(Integer, ForeignKey("inventory_items.id"))
    book_qty = Column(Integer, nullable=False)
    physical_qty = Column(Integer, nullable=False)
    variance = Column(Integer, nullable=False)          # physical - book
    created_at = Column(DateTime, default=datetime.utcnow)


class IndustrialDevice(Base):
    __tablename__ = "industrial_devices"
    __table_args__ = (
        UniqueConstraint("tenant_code", "device_code", name="uq_industrial_devices_tenant_device_code"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    device_code = Column(String, nullable=False)
    device_name = Column(String, nullable=False)
    device_type = Column(String, default="PLC")
    protocol = Column(String, default="MQTT")
    ip_address = Column(String, nullable=True)
    topic = Column(String, nullable=True)
    linked_machine_id = Column(Integer, ForeignKey("machines.id"), nullable=True)
    status = Column(String, default="Online")
    created_at = Column(DateTime, default=datetime.utcnow)


class IndustrialSignal(Base):
    __tablename__ = "industrial_signals"

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    device_id = Column(Integer, ForeignKey("industrial_devices.id"))
    machine_id = Column(Integer, ForeignKey("machines.id"), nullable=True)
    signal_name = Column(String, nullable=False)
    signal_value = Column(String, nullable=False)
    numeric_value = Column(Integer, default=0)
    unit = Column(String, nullable=True)
    quality = Column(String, default="Good")
    source_protocol = Column(String, default="MQTT")
    created_at = Column(DateTime, default=datetime.utcnow)


class PlcSignalMapping(Base):
    __tablename__ = "plc_signal_mappings"
    __table_args__ = (
        UniqueConstraint("tenant_code", "mapping_code", name="uq_plc_signal_mappings_tenant_mapping_code"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tenant_code = Column(String, index=True, nullable=False, default="DEFAULT")
    mapping_code = Column(String, nullable=False)
    device_id = Column(Integer, ForeignKey("industrial_devices.id"))
    source_signal = Column(String, nullable=False)
    mes_field = Column(String, nullable=False)
    transform_rule = Column(String, nullable=True)
    enabled = Column(String, default="Yes")
    created_at = Column(DateTime, default=datetime.utcnow)


# ── OEM platform (ADR-0017) ────────────────────────────────────────────
# A machine manufacturer that sells connected equipment into customer factories.
# These tables carry `oem_code` — a SECOND ownership dimension, independent of
# `tenant_code`. Nothing here changes an existing table: the OEM layer is
# additive, so an AMP database with no OEM rows behaves exactly as it did.


class OemOrganization(Base):
    """A machine manufacturer. The root of the OEM ownership dimension.

    `oem_code` is to OEM tables what `tenant_code` is to factory tables. It is
    deliberately a separate namespace: an OEM is not a tenant, does not appear in
    the tenant registry, and must never be reachable by binding a tenant.
    """

    __tablename__ = "oem_organizations"

    id = Column(Integer, primary_key=True, index=True)
    oem_code = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    # White-label configuration (ADR-0017). Same shape as TenantConfig's branding
    # so ONE frontend build serves every OEM from configuration, never a fork.
    brand_name = Column(String, nullable=True)
    brand_color = Column(String, default="#0f766e")
    brand_logo_url = Column(String, nullable=True)
    support_email = Column(String, nullable=True)
    support_phone = Column(String, nullable=True)
    # Revoking a whole organisation is one flag, checked at authentication.
    is_active = Column(Boolean, nullable=False, default=True, server_default=sa_true())
    created_at = Column(DateTime, default=datetime.utcnow)


class OemUser(Base):
    """An OEM principal. A SEPARATE TABLE from User, on purpose (ADR-0017).

    A factory administrator's user-management surface operates on `User`, so it
    cannot create, promote or disable an OEM login; an OEM administrator cannot
    mint a factory user. The two principal types cannot impersonate each other
    because they are not the same kind of row — not because a flag says so.
    """

    __tablename__ = "oem_users"

    id = Column(Integer, primary_key=True, index=True)
    oem_code = Column(String, index=True, nullable=False)
    username = Column(String, unique=True, nullable=False)
    password = Column(String, nullable=False)
    # OEM_ADMIN | OEM_SERVICE_MANAGER | OEM_SERVICE_ENGINEER | OEM_VIEWER.
    # Deliberately NOT the factory role strings: a token carrying "Admin" must
    # never satisfy an OEM role check, and vice versa.
    role = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, server_default=sa_true())
    created_at = Column(DateTime, default=datetime.utcnow)


class MachineModel(Base):
    """A product in an OEM's catalogue — what a serial number is an instance of.

    Scope is deliberately narrow: enough to manage a connected machine's
    lifecycle, not a general PLM. `telemetry_profile` is the configuration that
    keeps compressor-specific (or CNC-specific) signal vocabulary OUT of AMP core.
    """

    __tablename__ = "machine_models"
    __table_args__ = (
        UniqueConstraint("oem_code", "model_code", name="uq_machine_model_identity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    oem_code = Column(String, index=True, nullable=False)
    family = Column(String, nullable=False, default="")
    model_code = Column(String, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    rated_capacity = Column(String, nullable=True)
    rated_capacity_unit = Column(String, nullable=True)
    # Hours between scheduled services. NULL = this model has no hours-based
    # interval; the service engine must then say "not configured" rather than
    # invent a schedule.
    service_interval_hours = Column(Integer, nullable=True)
    warranty_months = Column(Integer, nullable=True)
    telemetry_profile = Column(Text, nullable=True)      # JSON; see ADR-0017
    documentation_url = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    status = Column(String, nullable=False, default="Active")   # Active | Discontinued
    created_at = Column(DateTime, default=datetime.utcnow)


class MachineInstallation(Base):
    """One physical machine an OEM built, wherever it now lives.

    THE DURABLE IDENTITY. `Machine` is UNIQUE(tenant_code, site, name) — a
    factory-local identity that dies the moment somebody renames the machine
    (ADR-0011). `serial_number` here is the identity that survives a rename, an
    IP change, or a new MQTT topic.

    UNIQUE PER OEM, NOT GLOBALLY. A global serial namespace would let one
    manufacturer discover another's fleet by probing for collisions; per-OEM
    uniqueness makes a guessed serial belong to nobody.

    TWO OWNERS, BOTH REAL. The factory owns the machine; the OEM owns the
    equipment relationship. This row carries BOTH codes and is filtered on
    whichever dimension the request bound — a factory sees its own installations,
    an OEM sees its own, and neither filter can widen the other. It is
    deliberately absent from tenancy.SCOPED_MODELS: the OEM sentinel tenant would
    filter it to nothing and the OEM could see no fleet at all.

    `machine_id` is NULLABLE because an installation exists from manufacture —
    before it is sold, shipped, installed, or linked to anything live. The link
    REFERENCES the factory's machine; it never replaces its identity, so MQTT
    ingest still resolves (tenant, site, name) exactly as ADR-0011 specifies.
    """

    __tablename__ = "machine_installations"
    __table_args__ = (
        UniqueConstraint("oem_code", "serial_number", name="uq_installation_serial"),
    )

    id = Column(Integer, primary_key=True, index=True)
    oem_code = Column(String, index=True, nullable=False)
    serial_number = Column(String, index=True, nullable=False)
    model_id = Column(Integer, ForeignKey("machine_models.id"), nullable=False)

    # Where it is installed. NULL = manufactured but not yet assigned to a
    # customer: it belongs to no factory and appears in no factory's view.
    #
    # NAMED `factory_tenant_code`, NOT `tenant_code`, AND THAT IS LOAD-BEARING.
    # Every other tenant_code in this file means "the tenant that OWNS this row".
    # Here it means "the counterparty" — the OEM owns this row. Three mechanisms
    # key off the literal attribute name `tenant_code`, and one of them destroys
    # data: offboard_tenant.purge_tenant_data sweeps EVERY mapper that has the
    # attribute and hard-deletes rows matching the departing tenant. With the
    # obvious name, offboarding Factory A would have deleted the OEM's own record
    # of machines it built and still owns. Offboarding UNLINKS an installation
    # (see offboard_tenant._unlink_oem_installations); it never deletes it.
    factory_tenant_code = Column(String, index=True, nullable=True)
    site = Column(String, nullable=False, default="", server_default="")
    machine_id = Column(Integer, ForeignKey("machines.id"), nullable=True)

    # Lifecycle: Manufactured -> Sold -> Assigned -> Installed -> Commissioning
    # -> Active -> Service -> Decommissioned.
    status = Column(String, nullable=False, default="Manufactured")
    installed_at = Column(DateTime, nullable=True)
    commissioned_at = Column(DateTime, nullable=True)
    decommissioned_at = Column(DateTime, nullable=True)

    warranty_start = Column(Date, nullable=True)
    warranty_end = Column(Date, nullable=True)

    # Operating hours as last reported — the OEM's service clock. NULL means
    # never reported, which is a different fact from zero.
    operating_hours = Column(Float, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    firmware_version = Column(String, nullable=True)

    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OemDataSharingPolicy(Base):
    """What a FACTORY has agreed to share with one OEM. Granted by the factory.

    DEFAULT DENY. No row means nothing is shared beyond what the OEM already
    knows from having sold the machine — its own serial, model, and which
    customer site it went to. There is no "the OEM has a relationship with
    Factory A, therefore the OEM may query Factory A": that inference is exactly
    what this table exists to prevent.

    `grants` is a CSV of grant keys (the TenantConfig.enabled_modules precedent).
    An empty string is a real and distinct choice — "this factory considered the
    question and shares nothing" — as against having no row at all.

    Read at QUERY TIME, never baked into a cached projection, so revoking a grant
    takes effect on the next request rather than whenever a projection next runs.
    """

    __tablename__ = "oem_data_sharing_policies"
    __table_args__ = (
        UniqueConstraint("oem_code", "tenant_code", name="uq_oem_sharing_policy"),
    )

    id = Column(Integer, primary_key=True, index=True)
    oem_code = Column(String, index=True, nullable=False)
    tenant_code = Column(String, index=True, nullable=False)
    grants = Column(String, nullable=False, default="")
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
