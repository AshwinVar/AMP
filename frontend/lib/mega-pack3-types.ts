export type CompanyTenant = {
  id: number;
  company_code: string;
  company_name: string;
  industry?: string | null;
  plan_name: string;
  subscription_status: string;
  seats: number;
  monthly_fee: number;
  created_at?: string;
};

export type SaaSAnalytics = {
  total_tenants: number;
  trial: number;
  active: number;
  past_due: number;
  cancelled: number;
  monthly_recurring_revenue: number;
  total_seats: number;
};

export type CostRecord = {
  id: number;
  cost_no: string;
  cost_type: string;
  reference_type?: string | null;
  reference_id?: number | null;
  description: string;
  amount: number;
  department?: string | null;
  created_at?: string;
};

export type CostingAnalytics = {
  total_cost_records: number;
  manual_cost_total: number;
  production_units: number;
  cost_per_good_unit: number;
  supplier_receipt_units: number;
  by_type: Record<string, number>;
  by_department: Record<string, number>;
};

export type OperatorJobExecution = {
  id: number;
  execution_no: string;
  operator_name: string;
  machine_id: number;
  work_order_id?: number | null;
  production_plan_id?: number | null;
  job_status: string;
  good_count: number;
  rejected_count: number;
  notes?: string | null;
  started_at?: string;
  completed_at?: string | null;
};

export type OperatorAnalytics = {
  total_jobs: number;
  started: number;
  paused: number;
  completed: number;
  good_count: number;
  rejected_count: number;
  quality_rate: number;
};
