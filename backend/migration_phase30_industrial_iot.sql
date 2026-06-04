CREATE TABLE IF NOT EXISTS industrial_devices (
    id SERIAL PRIMARY KEY,
    device_code VARCHAR UNIQUE NOT NULL,
    device_name VARCHAR NOT NULL,
    device_type VARCHAR DEFAULT 'PLC',
    protocol VARCHAR DEFAULT 'MQTT',
    ip_address VARCHAR,
    topic VARCHAR,
    linked_machine_id INTEGER REFERENCES machines(id),
    status VARCHAR DEFAULT 'Online',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS industrial_signals (
    id SERIAL PRIMARY KEY,
    device_id INTEGER REFERENCES industrial_devices(id),
    machine_id INTEGER REFERENCES machines(id),
    signal_name VARCHAR NOT NULL,
    signal_value VARCHAR NOT NULL,
    numeric_value INTEGER DEFAULT 0,
    unit VARCHAR,
    quality VARCHAR DEFAULT 'Good',
    source_protocol VARCHAR DEFAULT 'MQTT',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS plc_signal_mappings (
    id SERIAL PRIMARY KEY,
    mapping_code VARCHAR UNIQUE NOT NULL,
    device_id INTEGER REFERENCES industrial_devices(id),
    source_signal VARCHAR NOT NULL,
    mes_field VARCHAR NOT NULL,
    transform_rule VARCHAR,
    enabled VARCHAR DEFAULT 'Yes',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
