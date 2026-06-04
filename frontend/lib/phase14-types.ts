export type QualityInspection = {
  id: number;
  inspection_no: string;
  work_order_id?: number | null;
  production_plan_id?: number | null;
  machine_id?: number | null;
  inspector: string;
  inspected_quantity: number;
  passed_quantity: number;
  failed_quantity: number;
  defect_category?: string | null;
  rework_quantity: number;
  scrap_quantity: number;
  status: string;
  notes?: string | null;
  created_at?: string;
};

export type QualityAnalytics = {
  total_inspections: number;
  inspected_quantity: number;
  passed_quantity: number;
  failed_quantity: number;
  rework_quantity: number;
  scrap_quantity: number;
  pass_rate: number;
  fail_rate: number;
  defect_counts: Record<string, number>;
  machine_failures: Record<string, number>;
};
