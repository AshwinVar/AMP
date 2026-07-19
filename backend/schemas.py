from datetime import datetime, date
from pydantic import BaseModel
from typing import Optional


class MachineBase(BaseModel):
    name: str
    status: str
    utilization: int
    downtime: str


class MachineCreate(MachineBase):
    pass


class MachineResponse(MachineBase):
    id: int
    line: str = ""

    class Config:
        from_attributes = True


class DowntimeBase(BaseModel):
    machine_id: int
    reason: str
    duration: str
    notes: Optional[str] = None


class DowntimeCreate(DowntimeBase):
    pass


class DowntimeResponse(DowntimeBase):
    id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ShiftBase(BaseModel):
    shift_name: str
    target_output: int
    actual_output: int


class ShiftCreate(ShiftBase):
    pass


class ShiftResponse(ShiftBase):
    id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ProductionCreate(BaseModel):
    machine_id: int
    planned_minutes: int
    runtime_minutes: int
    ideal_cycle_time_seconds: int
    total_count: int
    good_count: int
    rejected_count: int


class ProductionResponse(ProductionCreate):
    id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AlertResponse(BaseModel):
    id: int
    alert_type: str
    severity: str
    message: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    username: str
    password: str
    role: str


class UserLogin(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class UserResponse(BaseModel):
    id: int
    username: str
    role: str

    class Config:
        from_attributes = True


class UserRoleUpdate(BaseModel):
    role: str


class MachineEventResponse(BaseModel):
    id: int
    machine_id: int
    machine_name: str
    old_status: Optional[str] = None
    new_status: str
    utilization: int
    source: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class WorkOrderCreate(BaseModel):
    work_order_no: str
    part_number: str
    batch_number: str
    machine_id: int
    target_quantity: int
    actual_quantity: int = 0
    status: str = "Planned"
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None


class WorkOrderUpdate(BaseModel):
    actual_quantity: Optional[int] = None
    status: Optional[str] = None


class WorkOrderResponse(BaseModel):
    id: int
    work_order_no: str
    part_number: str
    batch_number: str
    machine_id: int
    target_quantity: int
    actual_quantity: int
    status: str
    material_state: str = "RAW"
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ProductionPlanCreate(BaseModel):
    plan_no: str
    work_order_id: int
    machine_id: int
    planned_quantity: int
    actual_quantity: int = 0
    plan_date: date
    shift_name: str
    status: str = "Planned"


class ProductionPlanUpdate(BaseModel):
    actual_quantity: Optional[int] = None
    status: Optional[str] = None


class ProductionPlanResponse(BaseModel):
    id: int
    plan_no: str
    work_order_id: int
    machine_id: int
    planned_quantity: int
    actual_quantity: int
    plan_date: date
    shift_name: str
    status: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True



class EscalationCreate(BaseModel):
    machine_id: Optional[int] = None
    title: str
    severity: str
    owner: str
    department: str
    status: str = "Open"
    source: str = "Manual"
    notes: Optional[str] = None


class EscalationUpdate(BaseModel):
    status: Optional[str] = None
    owner: Optional[str] = None
    department: Optional[str] = None
    resolution_notes: Optional[str] = None


class EscalationResponse(BaseModel):
    id: int
    machine_id: Optional[int] = None
    title: str
    severity: str
    owner: str
    department: str
    status: str
    source: str
    notes: Optional[str] = None
    resolution_notes: Optional[str] = None
    created_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    class Config:
        from_attributes = True



class InventoryItemCreate(BaseModel):
    item_code: str
    item_name: str
    category: str
    supplier: Optional[str] = None
    unit: str
    current_stock: int = 0
    reorder_level: int = 0
    location: Optional[str] = None


class InventoryItemUpdate(BaseModel):
    item_name: Optional[str] = None
    category: Optional[str] = None
    supplier: Optional[str] = None
    unit: Optional[str] = None
    current_stock: Optional[int] = None
    reorder_level: Optional[int] = None
    location: Optional[str] = None


class InventoryItemResponse(BaseModel):
    id: int
    item_code: str
    item_name: str
    category: str
    supplier: Optional[str] = None
    unit: str
    current_stock: int
    reorder_level: int
    location: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class InventoryTransactionCreate(BaseModel):
    item_id: int
    transaction_type: str
    quantity: int
    reference: Optional[str] = None
    notes: Optional[str] = None


class InventoryTransactionResponse(BaseModel):
    id: int
    item_id: int
    transaction_type: str
    quantity: int
    reference: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True



class QualityInspectionCreate(BaseModel):
    inspection_no: str
    work_order_id: Optional[int] = None
    production_plan_id: Optional[int] = None
    machine_id: Optional[int] = None
    inspector: str
    inspected_quantity: int
    passed_quantity: int = 0
    failed_quantity: int = 0
    defect_category: Optional[str] = None
    rework_quantity: int = 0
    scrap_quantity: int = 0
    status: str = "Open"
    notes: Optional[str] = None


class QualityInspectionUpdate(BaseModel):
    passed_quantity: Optional[int] = None
    failed_quantity: Optional[int] = None
    defect_category: Optional[str] = None
    rework_quantity: Optional[int] = None
    scrap_quantity: Optional[int] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class QualityInspectionResponse(BaseModel):
    id: int
    inspection_no: str
    work_order_id: Optional[int] = None
    production_plan_id: Optional[int] = None
    machine_id: Optional[int] = None
    inspector: str
    inspected_quantity: int
    passed_quantity: int
    failed_quantity: int
    defect_category: Optional[str] = None
    rework_quantity: int
    scrap_quantity: int
    status: str
    notes: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True



class FactoryLayoutNodeCreate(BaseModel):
    machine_id: Optional[int] = None
    node_name: str
    node_type: str = "Machine"
    x_position: int = 50
    y_position: int = 50
    width: int = 160
    height: int = 100
    zone: str = "Production"


class FactoryLayoutNodeUpdate(BaseModel):
    machine_id: Optional[int] = None
    node_name: Optional[str] = None
    node_type: Optional[str] = None
    x_position: Optional[int] = None
    y_position: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    zone: Optional[str] = None


class FactoryLayoutNodeResponse(BaseModel):
    id: int
    machine_id: Optional[int] = None
    node_name: str
    node_type: str
    x_position: int
    y_position: int
    width: int
    height: int
    zone: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True



class CustomerOrderCreate(BaseModel):
    order_no: str
    customer_name: str
    product_name: str
    linked_work_order_id: Optional[int] = None
    linked_production_plan_id: Optional[int] = None
    order_quantity: int
    dispatched_quantity: int = 0
    priority: str = "Medium"
    due_date: date
    status: str = "Pending"
    notes: Optional[str] = None


class CustomerOrderUpdate(BaseModel):
    dispatched_quantity: Optional[int] = None
    priority: Optional[str] = None
    due_date: Optional[date] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    linked_work_order_id: Optional[int] = None
    linked_production_plan_id: Optional[int] = None


class CustomerOrderResponse(BaseModel):
    id: int
    order_no: str
    customer_name: str
    product_name: str
    linked_work_order_id: Optional[int] = None
    linked_production_plan_id: Optional[int] = None
    order_quantity: int
    dispatched_quantity: int
    priority: str
    due_date: date
    status: str
    notes: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True



class SupplierCreate(BaseModel):
    supplier_code: str
    supplier_name: str
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    category: Optional[str] = None
    status: str = "Active"


class SupplierUpdate(BaseModel):
    supplier_name: Optional[str] = None
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None


class SupplierResponse(BaseModel):
    id: int
    supplier_code: str
    supplier_name: str
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    category: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PurchaseOrderCreate(BaseModel):
    po_no: str
    supplier_id: int
    item_id: Optional[int] = None
    item_name: str
    order_quantity: int
    received_quantity: int = 0
    unit: str
    expected_delivery_date: date
    status: str = "Open"
    notes: Optional[str] = None


class PurchaseOrderUpdate(BaseModel):
    received_quantity: Optional[int] = None
    expected_delivery_date: Optional[date] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class PurchaseOrderResponse(BaseModel):
    id: int
    po_no: str
    supplier_id: int
    item_id: Optional[int] = None
    item_name: str
    order_quantity: int
    received_quantity: int
    unit: str
    expected_delivery_date: date
    status: str
    notes: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True



class ComplianceDocumentCreate(BaseModel):
    document_no: str
    title: str
    document_type: str
    department: str
    version: str = "1.0"
    owner: str
    approval_status: str = "Draft"
    review_due_date: date
    storage_link: Optional[str] = None
    notes: Optional[str] = None


class ComplianceDocumentUpdate(BaseModel):
    title: Optional[str] = None
    document_type: Optional[str] = None
    department: Optional[str] = None
    version: Optional[str] = None
    owner: Optional[str] = None
    approval_status: Optional[str] = None
    review_due_date: Optional[date] = None
    storage_link: Optional[str] = None
    notes: Optional[str] = None


class ComplianceDocumentResponse(BaseModel):
    id: int
    document_no: str
    title: str
    document_type: str
    department: str
    version: str
    owner: str
    approval_status: str
    review_due_date: date
    storage_link: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class MaintenanceTaskCreate(BaseModel):
    task_no: str
    machine_id: int
    task_type: str
    priority: str = "Medium"
    assigned_to: str
    planned_date: date
    completed_date: Optional[date] = None
    downtime_minutes: int = 0
    spare_parts_used: Optional[str] = None
    status: str = "Open"
    notes: Optional[str] = None


class MaintenanceTaskUpdate(BaseModel):
    priority: Optional[str] = None
    assigned_to: Optional[str] = None
    planned_date: Optional[date] = None
    completed_date: Optional[date] = None
    downtime_minutes: Optional[int] = None
    spare_parts_used: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class MaintenanceTaskResponse(BaseModel):
    id: int
    task_no: str
    machine_id: int
    task_type: str
    priority: str
    assigned_to: str
    planned_date: date
    completed_date: Optional[date] = None
    downtime_minutes: int
    spare_parts_used: Optional[str] = None
    status: str
    notes: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ProductionScheduleCreate(BaseModel):
    schedule_no: str
    work_order_id: Optional[int] = None
    production_plan_id: Optional[int] = None
    machine_id: int
    shift_name: str
    scheduled_date: date
    priority: str = "Medium"
    planned_quantity: int
    estimated_minutes: int = 480
    status: str = "Scheduled"
    notes: Optional[str] = None


class ProductionScheduleUpdate(BaseModel):
    machine_id: Optional[int] = None
    shift_name: Optional[str] = None
    scheduled_date: Optional[date] = None
    priority: Optional[str] = None
    planned_quantity: Optional[int] = None
    estimated_minutes: Optional[int] = None
    status: Optional[str] = None
    notes: Optional[str] = None


class ProductionScheduleResponse(BaseModel):
    id: int
    schedule_no: str
    work_order_id: Optional[int] = None
    production_plan_id: Optional[int] = None
    machine_id: int
    shift_name: str
    scheduled_date: date
    priority: str
    planned_quantity: int
    estimated_minutes: int
    status: str
    notes: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True



class IoTTelemetryCreate(BaseModel):
    machine_id: int
    signal_name: str
    signal_value: str
    numeric_value: int = 0
    unit: Optional[str] = None
    source: str = "MQTT"


class IoTTelemetryResponse(BaseModel):
    id: int
    machine_id: int
    signal_name: str
    signal_value: str
    numeric_value: int
    unit: Optional[str] = None
    source: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AIRecommendationCreate(BaseModel):
    recommendation_type: str
    severity: str = "Medium"
    title: str
    message: str
    related_machine_id: Optional[int] = None
    confidence: int = 75
    status: str = "Open"


class AIRecommendationUpdate(BaseModel):
    status: Optional[str] = None


class AIRecommendationResponse(BaseModel):
    id: int
    recommendation_type: str
    severity: str
    title: str
    message: str
    related_machine_id: Optional[int] = None
    confidence: int
    status: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True



class CompanyTenantCreate(BaseModel):
    company_code: str
    company_name: str
    industry: Optional[str] = None
    plan_name: str = "Starter"
    subscription_status: str = "Trial"
    seats: int = 5
    monthly_fee: int = 0


class CompanyTenantUpdate(BaseModel):
    company_name: Optional[str] = None
    industry: Optional[str] = None
    plan_name: Optional[str] = None
    subscription_status: Optional[str] = None
    seats: Optional[int] = None
    monthly_fee: Optional[int] = None


class CompanyTenantResponse(BaseModel):
    id: int
    company_code: str
    company_name: str
    industry: Optional[str] = None
    plan_name: str
    subscription_status: str
    seats: int
    monthly_fee: int
    created_at: Optional[datetime] = None
    trial_days_left: Optional[int] = None   # model property; None off-trial

    class Config:
        from_attributes = True


class CostRecordCreate(BaseModel):
    cost_no: str
    cost_type: str
    reference_type: Optional[str] = None
    reference_id: Optional[int] = None
    description: str
    amount: int = 0
    department: Optional[str] = None


class CostRecordUpdate(BaseModel):
    cost_type: Optional[str] = None
    description: Optional[str] = None
    amount: Optional[int] = None
    department: Optional[str] = None


class CostRecordResponse(BaseModel):
    id: int
    cost_no: str
    cost_type: str
    reference_type: Optional[str] = None
    reference_id: Optional[int] = None
    description: str
    amount: int
    department: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class OperatorJobExecutionCreate(BaseModel):
    execution_no: str
    operator_name: str
    machine_id: int
    work_order_id: Optional[int] = None
    production_plan_id: Optional[int] = None
    job_status: str = "Started"
    good_count: int = 0
    rejected_count: int = 0
    notes: Optional[str] = None


class OperatorJobExecutionUpdate(BaseModel):
    job_status: Optional[str] = None
    good_count: Optional[int] = None
    rejected_count: Optional[int] = None
    notes: Optional[str] = None


class OperatorJobExecutionResponse(BaseModel):
    id: int
    execution_no: str
    operator_name: str
    machine_id: int
    work_order_id: Optional[int] = None
    production_plan_id: Optional[int] = None
    job_status: str
    good_count: int
    rejected_count: int
    notes: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    class Config:
        from_attributes = True



class AuditLogCreate(BaseModel):
    actor: str = "system"
    action: str
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    details: Optional[str] = None


class AuditLogResponse(BaseModel):
    id: int
    actor: str
    action: str
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    details: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class NotificationCreate(BaseModel):
    notification_type: str
    severity: str = "Info"
    title: str
    message: str
    status: str = "Unread"


class NotificationUpdate(BaseModel):
    status: Optional[str] = None


class NotificationResponse(BaseModel):
    id: int
    notification_type: str
    severity: str
    title: str
    message: str
    status: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ReportRequestCreate(BaseModel):
    report_no: str
    report_type: str
    requested_by: str = "Admin"
    format: str = "PDF"
    status: str = "Generated"
    notes: Optional[str] = None


class ReportRequestResponse(BaseModel):
    id: int
    report_no: str
    report_type: str
    requested_by: str
    format: str
    status: str
    notes: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class IndustrialDeviceCreate(BaseModel):
    device_code: str
    device_name: str
    device_type: str = "PLC"
    protocol: str = "MQTT"
    ip_address: Optional[str] = None
    topic: Optional[str] = None
    linked_machine_id: Optional[int] = None
    status: str = "Online"


class IndustrialDeviceUpdate(BaseModel):
    status: Optional[str] = None
    linked_machine_id: Optional[int] = None


class IndustrialDeviceResponse(BaseModel):
    id: int
    device_code: str
    device_name: str
    device_type: str
    protocol: str
    ip_address: Optional[str] = None
    topic: Optional[str] = None
    linked_machine_id: Optional[int] = None
    status: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class IndustrialSignalCreate(BaseModel):
    device_id: int
    machine_id: Optional[int] = None
    signal_name: str
    signal_value: str
    numeric_value: int = 0
    unit: Optional[str] = None
    quality: str = "Good"
    source_protocol: str = "MQTT"


class IndustrialSignalResponse(BaseModel):
    id: int
    device_id: int
    machine_id: Optional[int] = None
    signal_name: str
    signal_value: str
    numeric_value: int
    unit: Optional[str] = None
    quality: str
    source_protocol: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PlcSignalMappingCreate(BaseModel):
    mapping_code: str
    device_id: int
    source_signal: str
    mes_field: str
    transform_rule: Optional[str] = None
    enabled: str = "Yes"


class PlcSignalMappingResponse(BaseModel):
    id: int
    mapping_code: str
    device_id: int
    source_signal: str
    mes_field: str
    transform_rule: Optional[str] = None
    enabled: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AgentPolicyUpdate(BaseModel):
    # The agent keys allowed to act without human approval for this tenant.
    auto_approve: list[str]
