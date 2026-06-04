export type Escalation = {
  id: number;
  machine_id?: number | null;
  title: string;
  severity: string;
  owner: string;
  department: string;
  status: string;
  source: string;
  notes?: string | null;
  resolution_notes?: string | null;
  created_at?: string;
  resolved_at?: string | null;
};

export type EscalationAnalytics = {
  total: number;
  open: number;
  in_progress: number;
  resolved: number;
  critical: number;
  high: number;
  medium: number;
  low: number;
};
