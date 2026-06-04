from datetime import date

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
