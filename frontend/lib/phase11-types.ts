export type ProductionPlan = {
  id: number;
  plan_no: string;
  work_order_id: number;
  machine_id: number;
  planned_quantity: number;
  actual_quantity: number;
  plan_date: string;
  shift_name: string;
  status: string;
  created_at?: string;
};

export type ProductionPlanAnalytics = {
  total_plans: number;
  planned_quantity: number;
  actual_quantity: number;
  /** Null when the denominator was empty — never 0, which is a real reading on this scale. */
  achievement: number | null;
  achievement_measured?: boolean;
  planned: number;
  running: number;
  completed: number;
  behind: number;
};
