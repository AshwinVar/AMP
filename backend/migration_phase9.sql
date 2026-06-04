CREATE TABLE IF NOT EXISTS work_orders (
    id SERIAL PRIMARY KEY,
    work_order_no VARCHAR UNIQUE NOT NULL,
    part_number VARCHAR NOT NULL,
    batch_number VARCHAR NOT NULL,
    machine_id INTEGER REFERENCES machines(id),
    target_quantity INTEGER NOT NULL,
    actual_quantity INTEGER DEFAULT 0,
    status VARCHAR DEFAULT 'Planned',
    planned_start TIMESTAMP,
    planned_end TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
