export type ComplianceDocument = {
  id: number;
  document_no: string;
  title: string;
  document_type: string;
  department: string;
  version: string;
  owner: string;
  approval_status: string;
  review_due_date: string;
  storage_link?: string | null;
  notes?: string | null;
  created_at?: string;
};

export type DocumentAnalytics = {
  total_documents: number;
  draft: number;
  approved: number;
  under_review: number;
  obsolete: number;
  review_due: number;
  type_counts: Record<string, number>;
  department_counts: Record<string, number>;
};

export type MaintenanceTask = {
  id: number;
  task_no: string;
  machine_id: number;
  task_type: string;
  priority: string;
  assigned_to: string;
  planned_date: string;
  completed_date?: string | null;
  downtime_minutes: number;
  spare_parts_used?: string | null;
  status: string;
  notes?: string | null;
  created_at?: string;
};

export type MaintenanceAnalytics = {
  total_tasks: number;
  open: number;
  in_progress: number;
  completed: number;
  overdue: number;
  preventive: number;
  breakdown: number;
  total_downtime_minutes: number;
  avg_repair_minutes: number;
  machine_counts: Record<string, number>;
};

export type ProductionSchedule = {
  id: number;
  schedule_no: string;
  work_order_id?: number | null;
  production_plan_id?: number | null;
  machine_id: number;
  shift_name: string;
  scheduled_date: string;
  priority: string;
  planned_quantity: number;
  estimated_minutes: number;
  status: string;
  notes?: string | null;
  created_at?: string;
};

export type ScheduleAnalytics = {
  total_schedules: number;
  scheduled: number;
  running: number;
  completed: number;
  delayed: number;
  total_quantity: number;
  total_minutes: number;
  machine_load: Record<string, number>;
  shift_load: Record<string, number>;
  bottlenecks: { machine: string; load_minutes: number }[];
};
