export type ExecutiveMachineOee = {
  machine_id: number;
  machine_name: string;
  status: string;
  availability: number;
  performance: number;
  quality: number;
  oee: number;
  downtime_minutes: number;
  total_count: number;
  good_count: number;
  rejected_count: number;
  utilization: number;
};

export type DowntimeParetoRow = {
  reason: string;
  minutes: number;
};

export type ShiftOeeRow = {
  shift_name: string;
  target_output: number;
  actual_output: number;
  efficiency: number;
};

export type QualityTrendRow = {
  defect: string;
  failed_quantity: number;
};

export type ExecutiveOee = {
  plant_availability: number;
  plant_performance: number;
  plant_quality: number;
  plant_oee: number;
  // Was there anything to measure? Computed by oee_contract.is_measurable
  // (#558) and published with the reason beside it in analytics_routes.py:
  // "0% and 'did not run' are different answers". The field existed on the
  // wire before it existed here, so no consumer could ask.
  has_data: boolean;
  machine_ranking: ExecutiveMachineOee[];
  downtime_pareto: DowntimeParetoRow[];
  shift_oee: ShiftOeeRow[];
  quality_trend: QualityTrendRow[];
  production_target: number;
  production_actual: number;
  production_achievement: number;
  running_machines: number;
  breakdown_machines: number;
  offline_machines: number;
};
