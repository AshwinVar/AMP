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
