export type CustomerOrder = {
  id: number;
  order_no: string;
  customer_name: string;
  product_name: string;
  linked_work_order_id?: number | null;
  linked_production_plan_id?: number | null;
  order_quantity: number;
  dispatched_quantity: number;
  priority: string;
  due_date: string;
  status: string;
  notes?: string | null;
  created_at?: string;
};

export type CustomerOrderAnalytics = {
  total_orders: number;
  pending: number;
  partial: number;
  dispatched: number;
  cancelled: number;
  late: number;
  total_order_qty: number;
  total_dispatched_qty: number;
  // null ("—") when nothing was ordered: a rate over an empty denominator is not
  // measured, not 0% (matches the backend's None + dispatch_measured).
  dispatch_rate: number | null;
  dispatch_measured?: boolean;
  priority_counts: Record<string, number>;
  customer_counts: Record<string, number>;
};
