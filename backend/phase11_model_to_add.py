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
