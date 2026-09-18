import type { Coverage } from "./coverage";

export type ExecutiveMachineOee = {
  machine_id: number;
  machine_name: string;
  status: string;
  /** null = not measured in the window (no planned time, no run, no counts). */
  availability: number | null;
  performance: number | null;
  quality: number | null;
  oee: number | null;
  /** The backend's own statement. false: the machine produced nothing, so it has
   *  no OEE — the row is here so the machine is listed, not so it is ranked. */
  measured: boolean;
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
  /** null for a shift with no target: nothing to measure against, not 0%. */
  efficiency: number | null;
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
  // How much of the plant plant_oee measured (OEE contract s4); see lib/coverage.
  coverage?: Coverage | null;
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
