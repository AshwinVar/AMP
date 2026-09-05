export type MachineEvent = {
  id: number;
  machine_id: number;
  machine_name: string;
  old_status?: string;
  new_status: string;
  utilization: number;
  source: string;
  created_at?: string;
};

export type MachineStateSummary = {
  machine_name: string;
  Running: number;
  Idle: number;
  Breakdown: number;
  Maintenance: number;
  // The fifth VALID_MACHINE_STATUS. Without it the stacked bars summed to
  // less than total_events for any machine whose gateway had dropped.
  Offline: number;
  total_events: number;
};
