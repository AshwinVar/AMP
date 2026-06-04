export type InventoryItem = {
  id: number;
  item_code: string;
  item_name: string;
  category: string;
  supplier?: string | null;
  unit: string;
  current_stock: number;
  reorder_level: number;
  location?: string | null;
  created_at?: string;
};

export type InventoryTransaction = {
  id: number;
  item_id: number;
  transaction_type: string;
  quantity: number;
  reference?: string | null;
  notes?: string | null;
  created_at?: string;
};

export type InventoryAnalytics = {
  total_items: number;
  low_stock_items: number;
  total_stock_units: number;
  transactions: number;
  category_counts: Record<string, number>;
  supplier_counts: Record<string, number>;
};
