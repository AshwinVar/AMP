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
  // ai/agents.py writes these two: every agent-raised escalation starts
  // Proposed (:306, :389), and rejecting one in the Approvals Inbox writes
  // Cancelled (:131). `other` catches a NULL status. Published so the row
  // accounts for `total`.
  proposed: number;
  cancelled: number;
  other: number;
  critical: number;
  high: number;
  medium: number;
  low: number;
};
