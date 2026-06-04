CREATE TABLE IF NOT EXISTS escalations (
    id SERIAL PRIMARY KEY,
    machine_id INTEGER REFERENCES machines(id),
    title VARCHAR NOT NULL,
    severity VARCHAR NOT NULL,
    owner VARCHAR NOT NULL,
    department VARCHAR NOT NULL,
    status VARCHAR DEFAULT 'Open',
    source VARCHAR DEFAULT 'Manual',
    notes VARCHAR,
    resolution_notes VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);
