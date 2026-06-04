CREATE TABLE IF NOT EXISTS production_plans (
    id SERIAL PRIMARY KEY,
    plan_no VARCHAR UNIQUE NOT NULL,
    work_order_id INTEGER REFERENCES work_orders(id),
    machine_id INTEGER REFERENCES machines(id),
    planned_quantity INTEGER NOT NULL,
    actual_quantity INTEGER DEFAULT 0,
    plan_date DATE NOT NULL,
    shift_name VARCHAR NOT NULL,
    status VARCHAR DEFAULT 'Planned',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
