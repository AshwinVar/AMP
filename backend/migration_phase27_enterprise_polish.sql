CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    actor VARCHAR DEFAULT 'system',
    action VARCHAR NOT NULL,
    entity_type VARCHAR,
    entity_id INTEGER,
    details VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notifications (
    id SERIAL PRIMARY KEY,
    notification_type VARCHAR NOT NULL,
    severity VARCHAR DEFAULT 'Info',
    title VARCHAR NOT NULL,
    message VARCHAR NOT NULL,
    status VARCHAR DEFAULT 'Unread',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS report_requests (
    id SERIAL PRIMARY KEY,
    report_no VARCHAR UNIQUE NOT NULL,
    report_type VARCHAR NOT NULL,
    requested_by VARCHAR DEFAULT 'Admin',
    format VARCHAR DEFAULT 'PDF',
    status VARCHAR DEFAULT 'Generated',
    notes VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
