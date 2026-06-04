CREATE TABLE IF NOT EXISTS quality_inspections (
    id SERIAL PRIMARY KEY,
    inspection_no VARCHAR UNIQUE NOT NULL,
    work_order_id INTEGER REFERENCES work_orders(id),
    production_plan_id INTEGER REFERENCES production_plans(id),
    machine_id INTEGER REFERENCES machines(id),
    inspector VARCHAR NOT NULL,
    inspected_quantity INTEGER NOT NULL,
    passed_quantity INTEGER DEFAULT 0,
    failed_quantity INTEGER DEFAULT 0,
    defect_category VARCHAR,
    rework_quantity INTEGER DEFAULT 0,
    scrap_quantity INTEGER DEFAULT 0,
    status VARCHAR DEFAULT 'Open',
    notes VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
