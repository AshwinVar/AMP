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

// GET /analytics/quality. Every figure is measured over ONE window — the
// canonical reporting week (backend quality_contract) — which the payload
// names, so the tiles can say which week they mean. The rates are null when
// the window inspected no units: 0% fail is the best value on the scale, and
// this endpoint used to publish it for a plant that had inspected nothing.
export type QualityAnalytics = {
  total_inspections: number;
  inspected_quantity: number;
  passed_quantity: number;
  failed_quantity: number;
  rework_quantity: number;
  scrap_quantity: number;
  pass_rate: number | null;
  fail_rate: number | null;
  measured?: boolean;
  /** "last 7 days" — the window every quality figure is pooled over. */
  window?: string;
  days?: number;
  defect_counts: Record<string, number>;
  machine_failures: Record<string, number>;
};
