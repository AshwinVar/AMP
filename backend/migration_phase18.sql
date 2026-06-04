CREATE TABLE IF NOT EXISTS suppliers (
    id SERIAL PRIMARY KEY,
    supplier_code VARCHAR UNIQUE NOT NULL,
    supplier_name VARCHAR NOT NULL,
    contact_person VARCHAR,
    email VARCHAR,
    phone VARCHAR,
    category VARCHAR,
    status VARCHAR DEFAULT 'Active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id SERIAL PRIMARY KEY,
    po_no VARCHAR UNIQUE NOT NULL,
    supplier_id INTEGER REFERENCES suppliers(id),
    item_id INTEGER REFERENCES inventory_items(id),
    item_name VARCHAR NOT NULL,
    order_quantity INTEGER NOT NULL,
    received_quantity INTEGER DEFAULT 0,
    unit VARCHAR NOT NULL,
    expected_delivery_date DATE NOT NULL,
    status VARCHAR DEFAULT 'Open',
    notes VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
