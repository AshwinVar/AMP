CREATE TABLE IF NOT EXISTS compliance_documents (
    id SERIAL PRIMARY KEY,
    document_no VARCHAR UNIQUE NOT NULL,
    title VARCHAR NOT NULL,
    document_type VARCHAR NOT NULL,
    department VARCHAR NOT NULL,
    version VARCHAR DEFAULT '1.0',
    owner VARCHAR NOT NULL,
    approval_status VARCHAR DEFAULT 'Draft',
    review_due_date DATE NOT NULL,
    storage_link VARCHAR,
    notes VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS maintenance_tasks (
    id SERIAL PRIMARY KEY,
    task_no VARCHAR UNIQUE NOT NULL,
    machine_id INTEGER REFERENCES machines(id),
    task_type VARCHAR NOT NULL,
    priority VARCHAR DEFAULT 'Medium',
    assigned_to VARCHAR NOT NULL,
    planned_date DATE NOT NULL,
    completed_date DATE,
    downtime_minutes INTEGER DEFAULT 0,
    spare_parts_used VARCHAR,
    status VARCHAR DEFAULT 'Open',
    notes VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS production_schedules (
    id SERIAL PRIMARY KEY,
    schedule_no VARCHAR UNIQUE NOT NULL,
    work_order_id INTEGER REFERENCES work_orders(id),
    production_plan_id INTEGER REFERENCES production_plans(id),
    machine_id INTEGER REFERENCES machines(id),
    shift_name VARCHAR NOT NULL,
    scheduled_date DATE NOT NULL,
    priority VARCHAR DEFAULT 'Medium',
    planned_quantity INTEGER NOT NULL,
    estimated_minutes INTEGER DEFAULT 480,
    status VARCHAR DEFAULT 'Scheduled',
    notes VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
