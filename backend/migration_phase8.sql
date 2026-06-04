CREATE TABLE IF NOT EXISTS machine_events (
    id SERIAL PRIMARY KEY,
    machine_id INTEGER REFERENCES machines(id),
    machine_name VARCHAR NOT NULL,
    old_status VARCHAR,
    new_status VARCHAR NOT NULL,
    utilization INTEGER DEFAULT 0,
    source VARCHAR DEFAULT 'mqtt',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
