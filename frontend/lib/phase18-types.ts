export type Supplier = {
  id: number;
  supplier_code: string;
  supplier_name: string;
  contact_person?: string | null;
  email?: string | null;
  phone?: string | null;
  category?: string | null;
  status: string;
  created_at?: string;
};

export type PurchaseOrder = {
  id: number;
  po_no: string;
  supplier_id: number;
  item_id?: number | null;
  item_name: string;
  order_quantity: number;
  received_quantity: number;
  unit: string;
  expected_delivery_date: string;
  status: string;
  notes?: string | null;
  created_at?: string;
};

export type PurchasingAnalytics = {
  suppliers: number;
  purchase_orders: number;
  // An agent proposal awaiting a human decision — deliberately its own
  // bucket, not folded into `open`, so the card does not present it as spend
  // already committed to a supplier.
  draft: number;
  open: number;
  partial: number;
  received: number;
  cancelled: number;
  // A status word the census does not recognise, or a NULL. Published so the
  // breakdown accounts for purchase_orders; normally 0.
  other: number;
  overdue: number;
  ordered_qty: number;
  received_qty: number;
  receipt_rate: number;
  supplier_pending: Record<string, number>;
};
