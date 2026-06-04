CREATE TABLE IF NOT EXISTS factory_layout_nodes (
    id SERIAL PRIMARY KEY,
    machine_id INTEGER REFERENCES machines(id),
    node_name VARCHAR NOT NULL,
    node_type VARCHAR DEFAULT 'Machine',
    x_position INTEGER DEFAULT 50,
    y_position INTEGER DEFAULT 50,
    width INTEGER DEFAULT 160,
    height INTEGER DEFAULT 100,
    zone VARCHAR DEFAULT 'Production',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
