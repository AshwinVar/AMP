CREATE TABLE IF NOT EXISTS iot_telemetry (
    id SERIAL PRIMARY KEY,
    machine_id INTEGER REFERENCES machines(id),
    signal_name VARCHAR NOT NULL,
    signal_value VARCHAR NOT NULL,
    numeric_value INTEGER DEFAULT 0,
    unit VARCHAR,
    source VARCHAR DEFAULT 'MQTT',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_recommendations (
    id SERIAL PRIMARY KEY,
    recommendation_type VARCHAR NOT NULL,
    severity VARCHAR DEFAULT 'Medium',
    title VARCHAR NOT NULL,
    message VARCHAR NOT NULL,
    related_machine_id INTEGER REFERENCES machines(id),
    confidence INTEGER DEFAULT 75,
    status VARCHAR DEFAULT 'Open',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
