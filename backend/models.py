from datetime import datetime
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Date
from sqlalchemy.orm import relationship

from database import Base


class Machine(Base):
    __tablename__ = "machines"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    status = Column(String, nullable=False)
    utilization = Column(Integer, default=0)
    downtime = Column(String, default="0 min")

    downtime_logs = relationship("DowntimeLog", back_populates="machine")
    production_records = relationship("ProductionRecord", back_populates="machine")


class DowntimeLog(Base):
    __tablename__ = "downtime_logs"

    id = Column(Integer, primary_key=True, index=True)
    machine_id = Column(Integer, ForeignKey("machines.id"))
    reason = Column(String, nullable=False)
    duration = Column(String, nullable=False)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    machine = relationship("Machine", back_populates="downtime_logs")


class ShiftData(Base):
    __tablename__ = "shift_data"

    id = Column(Integer, primary_key=True, index=True)
    shift_name = Column(String, nullable=False)
    target_output = Column(Integer, nullable=False)
    actual_output = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProductionRecord(Base):
    __tablename__ = "production_records"

    id = Column(Integer, primary_key=True, index=True)
    machine_id = Column(Integer, ForeignKey("machines.id"))
    planned_minutes = Column(Integer, nullable=False)
    runtime_minutes = Column(Integer, nullable=False)
    ideal_cycle_time_seconds = Column(Integer, nullable=False)
    total_count = Column(Integer, nullable=False)
    good_count = Column(Integer, nullable=False)
    rejected_count = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    machine = relationship("Machine", back_populates="production_records")


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
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


class MachineEvent(Base):
    __tablename__ = "machine_events"

    id = Column(Integer, primary_key=True, index=True)
    machine_id = Column(Integer, ForeignKey("machines.id"))
    machine_name = Column(String, nullable=False)
    old_status = Column(String, nullable=True)
    new_status = Column(String, nullable=False)
    utilization = Column(Integer, default=0)
    source = Column(String, default="mqtt")
    created_at = Column(DateTime, default=datetime.utcnow)


class WorkOrder(Base):
    __tablename__ = "work_orders"

    id = Column(Integer, primary_key=True, index=True)
    work_order_no = Column(String, unique=True, nullable=False)
    part_number = Column(String, nullable=False)
    batch_number = Column(String, nullable=False)
    machine_id = Column(Integer, ForeignKey("machines.id"))
    target_quantity = Column(Integer, nullable=False)
    actual_quantity = Column(Integer, default=0)
    status = Column(String, default="Planned")
    planned_start = Column(DateTime, nullable=True)
    planned_end = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProductionPlan(Base):
    __tablename__ = "production_plans"

    id = Column(Integer, primary_key=True, index=True)
    plan_no = Column(String, unique=True, nullable=False)
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

    id = Column(Integer, primary_key=True, index=True)
    item_code = Column(String, unique=True, nullable=False)
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
    item_id = Column(Integer, ForeignKey("inventory_items.id"))
    transaction_type = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    reference = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)



class QualityInspection(Base):
    __tablename__ = "quality_inspections"

    id = Column(Integer, primary_key=True, index=True)
    inspection_no = Column(String, unique=True, nullable=False)
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
    created_at = Column(DateTime, default=datetime.utcnow)



class FactoryLayoutNode(Base):
    __tablename__ = "factory_layout_nodes"

    id = Column(Integer, primary_key=True, index=True)
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

    id = Column(Integer, primary_key=True, index=True)
    order_no = Column(String, unique=True, nullable=False)
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

    id = Column(Integer, primary_key=True, index=True)
    supplier_code = Column(String, unique=True, nullable=False)
    supplier_name = Column(String, nullable=False)
    contact_person = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    category = Column(String, nullable=True)
    status = Column(String, default="Active")
    created_at = Column(DateTime, default=datetime.utcnow)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id = Column(Integer, primary_key=True, index=True)
    po_no = Column(String, unique=True, nullable=False)
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

    id = Column(Integer, primary_key=True, index=True)
    document_no = Column(String, unique=True, nullable=False)
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

    id = Column(Integer, primary_key=True, index=True)
    task_no = Column(String, unique=True, nullable=False)
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

    id = Column(Integer, primary_key=True, index=True)
    schedule_no = Column(String, unique=True, nullable=False)
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
    recommendation_type = Column(String, nullable=False)
    severity = Column(String, default="Medium")
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)
    related_machine_id = Column(Integer, ForeignKey("machines.id"), nullable=True)
    confidence = Column(Integer, default=75)
    status = Column(String, default="Open")
    created_at = Column(DateTime, default=datetime.utcnow)



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


class CostRecord(Base):
    __tablename__ = "cost_records"

    id = Column(Integer, primary_key=True, index=True)
    cost_no = Column(String, unique=True, nullable=False)
    cost_type = Column(String, nullable=False)
    reference_type = Column(String, nullable=True)
    reference_id = Column(Integer, nullable=True)
    description = Column(String, nullable=False)
    amount = Column(Integer, default=0)
    department = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class OperatorJobExecution(Base):
    __tablename__ = "operator_job_executions"

    id = Column(Integer, primary_key=True, index=True)
    execution_no = Column(String, unique=True, nullable=False)
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
    actor = Column(String, default="system")
    action = Column(String, nullable=False)
    entity_type = Column(String, nullable=True)
    entity_id = Column(Integer, nullable=True)
    details = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    notification_type = Column(String, nullable=False)
    severity = Column(String, default="Info")
    title = Column(String, nullable=False)
    message = Column(String, nullable=False)
    status = Column(String, default="Unread")
    created_at = Column(DateTime, default=datetime.utcnow)


class ReportRequest(Base):
    __tablename__ = "report_requests"

    id = Column(Integer, primary_key=True, index=True)
    report_no = Column(String, unique=True, nullable=False)
    report_type = Column(String, nullable=False)
    requested_by = Column(String, default="Admin")
    format = Column(String, default="PDF")
    status = Column(String, default="Generated")
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class IndustrialDevice(Base):
    __tablename__ = "industrial_devices"

    id = Column(Integer, primary_key=True, index=True)
    device_code = Column(String, unique=True, nullable=False)
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

    id = Column(Integer, primary_key=True, index=True)
    mapping_code = Column(String, unique=True, nullable=False)
    device_id = Column(Integer, ForeignKey("industrial_devices.id"))
    source_signal = Column(String, nullable=False)
    mes_field = Column(String, nullable=False)
    transform_rule = Column(String, nullable=True)
    enabled = Column(String, default="Yes")
    created_at = Column(DateTime, default=datetime.utcnow)
