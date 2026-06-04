export type PredictiveRisk = {
  machine_id: number;
  machine_name: string;
  status: string;
  utilization: number;
  risk_score: number;
  risk_level: string;
  downtime_minutes: number;
  downtime_events: number;
  breakdown_events: number;
  reject_rate: number;
  work_order_pressure: number;
  reasons: string[];
  recommendation: string;
};
