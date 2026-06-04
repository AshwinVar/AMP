export type FactoryLayoutNode = {
  id: number;
  machine_id?: number | null;
  node_name: string;
  node_type: string;
  x_position: number;
  y_position: number;
  width: number;
  height: number;
  zone: string;
  created_at?: string;
};

export type FactoryZoneSummary = {
  zone: string;
  nodes: number;
  running: number;
  breakdown: number;
  idle: number;
  maintenance: number;
};

export type FactoryCommandCenter = {
  machines: number;
  running: number;
  breakdown: number;
  idle: number;
  maintenance: number;
  total_downtime_minutes: number;
  active_work_orders: number;
  behind_plans: number;
  open_escalations: number;
  low_stock_items: number;
  quality_fail_rate: number;
  zone_summary: FactoryZoneSummary[];
};
