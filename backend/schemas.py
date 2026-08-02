from datetime import datetime, date
from pydantic import BaseModel, field_validator
from typing import Optional


def _coalesce_null_count(value):
    """Heal a NULL count column to the column's own default of 0 on the way OUT.

    Several counted columns are declared ``Column(Integer, default=0)`` WITHOUT
    ``nullable=False`` (ProductionPlan.actual_quantity, OperatorJobExecution
    good_count/rejected_count). The ORM default only fills a value the *inserter*
    omitted, so a row written by raw SQL, a migration, or a PATCH that cleared the
    field can legitimately hold NULL. These response models type the same fields
    as a non-optional ``int``, so a single such NULL row raised Pydantic
    ``ValidationError`` and 500-ed the WHOLE list endpoint (GET /production-plans,
    GET /operator/executions) — one bad row hiding every good one.

    NULL here means "no value recorded", and the column's declared default is 0,
    so coalesce to 0 — the same honest choice the operator-execution PATCH already
    makes in-handler (``row.good_count = row.good_count or 0``). Applied
    ``mode="before"`` so it runs on the raw attribute value ahead of int
    validation. A real recorded 0 is untouched; only NULL is healed.
    """
    return 0 if value is None else value


def _coalesce_null_text(default: str):
    """Factory: heal a NULL text column to its declared default on the way OUT.

    The string-column twin of ``_coalesce_null_count``. ``Machine.downtime``
    (``default="0 min"``) and ``Machine.line`` (``default=""``) are declared
    ``Column(String, default=...)`` WITHOUT ``nullable=False``, so the ORM default
    only fills a value the *inserter* omitted — a row written by raw SQL, a
    migration, or an update that cleared the field can legitimately hold NULL.
    ``MachineResponse`` types both as a non-optional ``str``, so a single such NULL
    row raised Pydantic ``ValidationError`` and 500-ed the WHOLE ``GET /machines``
    roster — one bad row hiding every good machine, on the core endpoint every
    dashboard polls. Coalesce a NULL to the column's declared default (the honest
    "no value recorded" reading); a real recorded value is untouched.
    """
    def _coalesce(value):
        return default if value is None else value
    return _coalesce


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

    # utilization / downtime / line are Column(..., default=...) WITHOUT
    # nullable=False, so a raw-SQL / migration / cleared-field row can hold NULL.
    # These are typed non-optional here, so one such row raised ValidationError and
    # 500-ed the entire GET /machines list (the roster every dashboard reads) —
    # the same response-serialisation NULL class already healed on the
    # production-plan / operator-execution lists (#375). Coalesce each NULL to the
    # column's own declared default on the way out; a real value is untouched.
    _heal_utilization = field_validator("utilization", mode="before")(_coalesce_null_count)
    _heal_downtime = field_validator("downtime", mode="before")(_coalesce_null_text("0 min"))
    _heal_line = field_validator("line", mode="before")(_coalesce_null_text(""))

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

    # WorkOrder.actual_quantity is Column(Integer, default=0) WITHOUT
    # nullable=False — the same profile as ProductionPlan.actual_quantity above —
    # so a row written by raw SQL, a migration, or a cleared write can hold a true
    # NULL, and this non-optional int would raise ValidationError and 500 the WHOLE
    # GET /work-orders list on a single such row (one bad row hiding every good
    # one, the class already healed for GET /production-plans, /inventory/items
    # #423, /customer-orders #631, quality). This response model was the one that
    # was missed; coalesce a NULL to the column's own default of 0, exactly as its
    # sibling ProductionPlanResponse.actual_quantity does. target_quantity is
    # nullable=False (a required order size, not a default-0 count), so — like
    # planned_quantity / order_quantity / inspected_quantity — it is left unhealed.
    _heal_actual_quantity = field_validator("actual_quantity", mode="before")(
        _coalesce_null_count
    )

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

    _heal_actual_quantity = field_validator("actual_quantity", mode="before")(
        _coalesce_null_count
    )

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

    # status (default="Open") and source (default="Manual") are Column(String,
    # default=...) WITHOUT nullable=False, so a raw-SQL / migration / cleared-field
    # row can hold a genuine NULL. Both are typed non-optional here, so one such row
    # raised Pydantic ValidationError during response serialisation and 500-ed the
    # ENTIRE GET /escalations backlog — one bad row hiding every open issue, on an
    # endpoint every dashboard polls. Same response-serialisation NULL class already
    # healed on the machines roster (#395) and the production-plan / operator /
    # customer-order / purchase-order lists (#375/#401/#423). Coalesce each NULL to
    # the column's own declared default on the way out — and a NULL status is exactly
    # the "still open" case every escalation READER already treats as open
    # (or_(status.is_(None), status != 'Resolved'): #295/#403), so healing it to
    # "Open" keeps the list reader and the counters on the same basis. A real value
    # is untouched.
    _heal_status = field_validator("status", mode="before")(_coalesce_null_text("Open"))
    _heal_source = field_validator("source", mode="before")(_coalesce_null_text("Manual"))

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

    # current_stock / reorder_level are Column(Integer, default=0) WITHOUT
    # nullable=False, so a row written by raw SQL / a migration / a cleared update
    # can hold a true NULL. They are typed non-optional here, so a single such NULL
    # row raised Pydantic ValidationError during response serialisation and 500-ed
    # the WHOLE GET /inventory/items list — one bad row hiding every good item, on
    # an endpoint the inventory dashboards poll. The compute paths already coalesce
    # this exact NULL (the PATCH heals it in-handler, the stock-mutation math reads
    # `stock or 0`); the raw-ORM list serializer was the one reader left un-healed
    # (same #375/#395/#401 response-serialisation heal as CustomerOrder/PurchaseOrder
    # /OperatorJobExecution above). Coalesce a NULL to the column's own default of 0
    # on the way out; a real 0 is untouched (mode="before" only rewrites None).
    _heal_counts = field_validator("current_stock", "reorder_level", mode="before")(
        _coalesce_null_count
    )

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

    # passed / failed / rework / scrap_quantity are Column(Integer, default=0)
    # WITHOUT nullable=False; inspected_quantity is Column(Integer, nullable=False)
    # WITHOUT a default. Neither constraint protects a row written by raw SQL / a
    # migration / a legacy insert — a nullable=False column is NOT retro-applied to
    # pre-existing rows, which is exactly why analytics_engine coalesces its own
    # nullable=False count columns (ProductionRecord.total_count, ShiftData
    # .target_output — `... or 0` in calculate_oee_from_record / build_shift_kpis).
    # All five fields are typed non-optional here, so a single NULL row raised
    # Pydantic ValidationError during response serialisation and 500-ed the WHOLE GET
    # /quality/inspections list — one bad row hiding every good inspection. The
    # compute paths already coalesce these NULLs (the PATCH heals them in-handler,
    # /analytics/quality COALESCEs in SQL, the defect generator reads `failed or 0`);
    # the raw-ORM list serializer was left half-healed — #423 covered the four
    # default-0 counts but skipped inspected_quantity on the same (mistaken)
    # "nullable=False needs no heal" reasoning that its own PATCH sibling used.
    # Coalesce a NULL to 0 on the way out ("no count recorded"), matching every other
    # reader; a real 0 is untouched (mode="before" only rewrites None).
    _heal_counts = field_validator(
        "inspected_quantity",
        "passed_quantity", "failed_quantity", "rework_quantity", "scrap_quantity",
        mode="before",
    )(_coalesce_null_count)

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

    # dispatched_quantity is Column(Integer, default=0) WITHOUT nullable=False, so a
    # row written by raw SQL / a migration / a cleared update can hold a true NULL.
    # It is typed non-optional here, so a single such NULL row raised Pydantic
    # ValidationError during response serialisation and 500-ed the WHOLE GET
    # /customer-orders list — one bad row hiding every good order, on an endpoint the
    # order-book dashboard polls. The compute paths already coalesce this exact NULL
    # (the PATCH heals it in-handler, /analytics/customer-orders COALESCEs it, the
    # CSV export reads `o.dispatched_quantity or 0`); the raw-ORM list serializer was
    # the one reader left un-healed. Coalesce a NULL to the column's own default of 0
    # on the way out — the same #375/#395 response-serialisation heal; a real 0 is
    # untouched (mode="before" only rewrites None).
    _heal_dispatched_quantity = field_validator("dispatched_quantity", mode="before")(_coalesce_null_count)

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

    # received_quantity is Column(Integer, default=0) WITHOUT nullable=False (the exact
    # sibling of CustomerOrder.dispatched_quantity above), so a raw-SQL / migration /
    # cleared-field row can hold a true NULL. Typed non-optional here, so one such row
    # raised Pydantic ValidationError during response serialisation and 500-ed the
    # WHOLE GET /purchase-orders list. Every compute path already coalesces it (the
    # PATCH heals it in-handler, /analytics/purchasing COALESCEs it, the CSV export
    # reads `p.received_quantity or 0`); the raw-ORM list serializer was the one reader
    # left un-healed. Coalesce a NULL to the column's own default of 0 on the way out,
    # matching the #375/#395 heals; a real recorded 0 is untouched.
    _heal_received_quantity = field_validator("received_quantity", mode="before")(_coalesce_null_count)

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

    # status (default="Open"), priority (default="Medium") and downtime_minutes
    # (default=0) are declared Column(..., default=...) WITHOUT nullable=False, so
    # the ORM default only fills a value the *inserter* omitted — a row written by
    # raw SQL, a migration, or an update that cleared the field can legitimately
    # hold NULL. All three are typed non-optional here, so a single such NULL row
    # raised Pydantic ValidationError during response serialisation and 500-ed the
    # WHOLE GET /maintenance/tasks list — one bad row hiding every good task. This
    # is the same response-serialisation NULL class already healed on the machine /
    # production-plan / operator-execution lists (#375/#395) and on EscalationResponse
    # (which likewise heals a NULL status to its "Open" default).
    #
    # Coalesce each NULL to the column's own declared default on the way out. The
    # values also keep the list on the SAME basis as /analytics/maintenance
    # (rule-3): that rollup treats a NULL status as not-Completed (still overdue/open
    # — it OR's `status IS NULL` into the overdue filter) and reads a NULL
    # downtime_minutes as 0 (COALESCE(SUM(downtime_minutes), 0)), so healing status
    # -> "Open" and downtime -> 0 makes the per-task row agree with the headline it
    # sums into. A real recorded value — including a real 0 downtime — is untouched.
    _heal_status = field_validator("status", mode="before")(_coalesce_null_text("Open"))
    _heal_priority = field_validator("priority", mode="before")(_coalesce_null_text("Medium"))
    _heal_downtime_minutes = field_validator("downtime_minutes", mode="before")(_coalesce_null_count)

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

    # ProductionSchedule.estimated_minutes is Column(Integer, default=480) WITHOUT
    # nullable=False, so — like every other nullable-default int on the sibling
    # response models above — a row written by raw SQL / a migration / a PATCH that
    # cleared the field can legitimately hold NULL (and ProductionScheduleUpdate
    # types it Optional[int]=None, so PATCH {"estimated_minutes": null} persists a
    # NULL through the API alone). This field is typed a non-optional int, so one
    # such NULL row raised Pydantic ValidationError during serialisation and 500-ed
    # BOTH the PATCH response AND every subsequent GET /production-schedules for the
    # tenant — one poisoned row hiding all schedules, the same class already healed
    # on the plan / operator / order / machine lists. It was the only counted
    # response schema still missing the heal.
    #
    # Heals to 0 (NOT the column's 480 default): both readers of this column already
    # treat a NULL as 0 — the /analytics/production-schedules rollup does
    # COALESCE(SUM(estimated_minutes), 0) and the schedule-load read-model does
    # `s.estimated_minutes or 0`. Showing 0 for a NULL row keeps the per-schedule
    # list on the SAME basis as the booked-minutes headline and per-machine load it
    # sums into (rule-3: the parts reconcile with the whole). A real recorded value —
    # including a real 0 — is untouched; only NULL is rewritten.
    _heal_estimated_minutes = field_validator("estimated_minutes", mode="before")(
        _coalesce_null_count
    )

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

    # numeric_value is Column(Integer, default=0) WITHOUT nullable=False, so a row
    # written by raw SQL / a migration / a legacy insert can hold NULL (the ORM
    # default only fills a value the inserter omitted). Typed non-optional here, a
    # single such NULL row raised Pydantic ValidationError during response
    # serialisation and 500-ed the WHOLE GET /iot/telemetry list — one bad reading
    # hiding every good one. Same class already healed on the machine / quality /
    # maintenance / operator-execution lists (#423/#447). Coalesce a NULL to the
    # column's own default of 0 ("no numeric reading"); a real recorded 0 is
    # untouched (mode="before" only rewrites None).
    _heal_numeric_value = field_validator("numeric_value", mode="before")(
        _coalesce_null_count
    )

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

    _heal_counts = field_validator("good_count", "rejected_count", mode="before")(
        _coalesce_null_count
    )
    # job_status is Column(String, default="Started") WITHOUT nullable=False, and
    # OperatorJobExecutionUpdate types it Optional[str]=None — so a client can PATCH
    # an explicit {"job_status": null} (the update handler's setattr loop over
    # exclude_unset persists it) and a legacy / raw-SQL / migration row can already
    # carry NULL. Left as None the response 500s (this field is a non-optional str),
    # and because the NULL is committed first, every subsequent
    # GET /operator/executions then 500s for the whole tenant until the row is
    # repaired. The sibling count columns above were healed but this string column
    # was missed. Coalesce a NULL to the column's own default "Started" — the same
    # heal EscalationResponse (status -> "Open") and MaintenanceTaskResponse
    # (status -> "Open", priority -> "Medium") already apply to their nullable
    # string-default columns. A real recorded status is untouched (mode="before"
    # only rewrites None).
    _heal_job_status = field_validator("job_status", mode="before")(
        _coalesce_null_text("Started")
    )

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

    # numeric_value is Column(Integer, default=0) WITHOUT nullable=False (the same
    # gap as IoTTelemetryResponse above): a raw-SQL / migration / legacy row can
    # hold NULL, and typed non-optional here it raised Pydantic ValidationError and
    # 500-ed the WHOLE GET /industrial/signals list — one bad signal hiding every
    # good one. Heal a NULL to the column's declared default of 0; a real 0 stays 0.
    _heal_numeric_value = field_validator("numeric_value", mode="before")(
        _coalesce_null_count
    )

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
