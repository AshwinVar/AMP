export type WorkOrder = {
  id: number;
  work_order_no: string;
  part_number: string;
  batch_number: string;
  machine_id: number;
  target_quantity: number;
  actual_quantity: number;
  status: string;
  planned_start?: string;
  planned_end?: string;
  created_at?: string;
};

export type WorkOrderAnalytics = {
  total_work_orders: number;
  planned: number;
  running: number;
  completed: number;
  delayed: number;
  total_target: number;
  total_actual: number;
  achievement: number;
};
