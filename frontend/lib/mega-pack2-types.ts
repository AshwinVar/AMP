export type IoTTelemetry = {
  id: number;
  machine_id: number;
  signal_name: string;
  signal_value: string;
  numeric_value: number;
  unit?: string | null;
  source: string;
  created_at?: string;
};

export type IoTCommandCenter = {
  machines: number;
  signals: number;
  live_machines: number;
  latest_signals: {
    machine_id: number;
    machine_name: string;
    signal_name: string;
    signal_value: string;
    numeric_value: number;
    unit?: string | null;
    source: string;
    created_at?: string;
  }[];
};

export type AIRecommendation = {
  id: number;
  recommendation_type: string;
  severity: string;
  title: string;
  message: string;
  related_machine_id?: number | null;
  confidence: number;
  status: string;
  created_at?: string;
  // ADR-0039. What to do about it, always a sentence — this queue's only
  // futures were Acknowledged and Closed, so "AI Predictive Intelligence"
  // suggested things and offered no way to act on any of them. `propose` is
  // the stricter thing: a draft AMP can actually carry out. Most
  // recommendations have none, because ordering stock and rebalancing a
  // schedule are not AMP's to do.
  action?: string | null;
  propose?: { kind: "maintenance_task"; machine_id: number } | null;
};

export type AIInsights = {
  total: number;
  open: number;
  acknowledged: number;
  closed: number;
  critical: number;
  high: number;
  medium: number;
  low: number;
};
