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
  dispatch_rate: number;
  priority_counts: Record<string, number>;
  customer_counts: Record<string, number>;
};
