export type AuditLog = {
  id: number;
  actor: string;
  action: string;
  entity_type?: string | null;
  entity_id?: number | null;
  details?: string | null;
  created_at?: string;
};

export type NotificationItem = {
  id: number;
  notification_type: string;
  severity: string;
  title: string;
  message: string;
  status: string;
  created_at?: string;
};

export type ReportRequest = {
  id: number;
  report_no: string;
  report_type: string;
  requested_by: string;
  format: string;
  status: string;
  notes?: string | null;
  created_at?: string;
};

export type SystemHealth = {
  api_status: string;
  database_status: string;
  machines: number;
  users: number;
  alerts: number;
  open_escalations: number;
  unread_notifications: number;
  audit_logs: number;
  modules_enabled: string[];
};

export type FinalExecutiveSummary = {
  machine_count: number;
  running_machines: number;
  work_orders: number;
  production_plans: number;
  /** Null when the denominator was empty — never 0, which is a real reading on this scale. */
  quality_rate: number | null;
  quality_measured?: boolean;
  /** "last 7 days" — the same window every other quality figure is pooled over. */
  quality_window?: string;
  low_stock_items: number;
  customer_orders: number;
  /** Null when the denominator was empty — never 0, which is a real reading on this scale. */
  dispatch_rate: number | null;
  dispatch_measured?: boolean;
  purchase_orders: number;
  /** A SUM, not a ratio: money not spent really is zero money. Stays a number. */
  total_cost: number;
};
