export type Role = "Admin" | "Supervisor" | "Operator";

export type Machine = {
  id: number;
  name: string;
  status: string;
  utilization: number;
  downtime: string;
};

export type DowntimeLog = {
  id: number;
  machine_id: number;
  reason: string;
  duration: string;
  notes?: string;
  created_at?: string;
};

export type Shift = {
  id: number;
  shift_name: string;
  target_output: number;
  actual_output: number;
  created_at?: string;
};

export type ProductionRecord = {
  id: number;
  machine_id: number;
  planned_minutes: number;
  runtime_minutes: number;
  ideal_cycle_time_seconds: number;
  total_count: number;
  good_count: number;
  rejected_count: number;
  created_at?: string;
};

export type OeeRow = {
  id: number;
  machine_id: number;
  machine_name: string;
  availability: number;
  performance: number;
  quality: number;
  oee: number;
  created_at?: string;
};

export type User = {
  id: number;
  username: string;
  role: Role;
};

export type Summary = {
  machines: number;
  running: number;
  idle: number;
  breakdown: number;
  maintenance: number;
  avg_utilization: number;
  avg_oee: number;
  avg_availability: number;
  avg_performance: number;
  avg_quality: number;
  downtime_events: number;
  total_downtime_minutes: number;
  avg_shift_efficiency: number;
  top_reason: string;
  top_machine: string;
  reason_counts: Record<string, number>;
  alerts: { type: string; severity: string; message: string }[];
};
