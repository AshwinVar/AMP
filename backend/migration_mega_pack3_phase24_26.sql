CREATE TABLE IF NOT EXISTS company_tenants (
    id SERIAL PRIMARY KEY,
    company_code VARCHAR UNIQUE NOT NULL,
    company_name VARCHAR NOT NULL,
    industry VARCHAR,
    plan_name VARCHAR DEFAULT 'Starter',
    subscription_status VARCHAR DEFAULT 'Trial',
    seats INTEGER DEFAULT 5,
    monthly_fee INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cost_records (
    id SERIAL PRIMARY KEY,
    cost_no VARCHAR UNIQUE NOT NULL,
    cost_type VARCHAR NOT NULL,
    reference_type VARCHAR,
    reference_id INTEGER,
    description VARCHAR NOT NULL,
    amount INTEGER DEFAULT 0,
    department VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS operator_job_executions (
    id SERIAL PRIMARY KEY,
    execution_no VARCHAR UNIQUE NOT NULL,
    operator_name VARCHAR NOT NULL,
    machine_id INTEGER REFERENCES machines(id),
    work_order_id INTEGER REFERENCES work_orders(id),
    production_plan_id INTEGER REFERENCES production_plans(id),
    job_status VARCHAR DEFAULT 'Started',
    good_count INTEGER DEFAULT 0,
    rejected_count INTEGER DEFAULT 0,
    notes VARCHAR,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);
