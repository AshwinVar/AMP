CREATE TABLE IF NOT EXISTS customer_orders (
    id SERIAL PRIMARY KEY,
    order_no VARCHAR UNIQUE NOT NULL,
    customer_name VARCHAR NOT NULL,
    product_name VARCHAR NOT NULL,
    linked_work_order_id INTEGER REFERENCES work_orders(id),
    linked_production_plan_id INTEGER REFERENCES production_plans(id),
    order_quantity INTEGER NOT NULL,
    dispatched_quantity INTEGER DEFAULT 0,
    priority VARCHAR DEFAULT 'Medium',
    due_date DATE NOT NULL,
    status VARCHAR DEFAULT 'Pending',
    notes VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
