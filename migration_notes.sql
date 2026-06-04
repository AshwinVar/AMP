-- Run only if backend errors because an existing table is missing created_at.
-- Open pgAdmin or psql and run against flowmes_db.

ALTER TABLE downtime_logs
ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE shift_data
ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

CREATE TABLE IF NOT EXISTS production_records (
    id SERIAL PRIMARY KEY,
    machine_id INTEGER REFERENCES machines(id),
    planned_minutes INTEGER NOT NULL,
    runtime_minutes INTEGER NOT NULL,
    ideal_cycle_time_seconds INTEGER NOT NULL,
    total_count INTEGER NOT NULL,
    good_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alerts (
    id SERIAL PRIMARY KEY,
    alert_type VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    message VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
