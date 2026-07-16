"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend inventory summary (ai/inventory.py build_inventory_summary).
type Item = {
  item_code: string;
  item_name: string;
  current_stock: number;
  reorder_level: number;
  unit: string;
  supplier: string | null;
  coverage: number;
  out_of_stock: boolean;
};

type InventorySummary = {
  total_items: number;
  at_risk: number;
  out_of_stock: number;
  items: Item[];
  auto_pos_pending: number;
  auto_pos: { po_no: string; item_name: string; order_quantity: number; unit: string; expected_delivery_date: string | null }[];
};

function coverageColor(cov: number, out: boolean) {
  if (out) return "bg-red-500";
  if (cov <= 50) return "bg-orange-500";
  if (cov <= 100) return "bg-yellow-500";
  return "bg-emerald-500";
}

// A glanceable supply-risk read-out — the items below their reorder level and
// the Reorder agent's drafted POs. Self-contained: fetches its own summary and
// refreshes, so it drops onto any screen without prop-drilling. Renders nothing
// when there's no risk and nothing awaiting approval.
export default function InventorySnapshot() {
  const [inv, setInv] = useState<InventorySummary | null>(null);

  const load = useCallback(async () => {
    try {
      setInv(await apiGet<InventorySummary>("/inventory-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!inv || (inv.at_risk === 0 && inv.auto_pos_pending === 0)) return null;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Supply risk · inventory</h3>
          <p className="text-slate-400 text-sm mt-1">
            {inv.at_risk} of {inv.total_items} item{inv.total_items !== 1 ? "s" : ""} at or below reorder level
            {inv.auto_pos_pending > 0 && ` · ${inv.auto_pos_pending} agent PO${inv.auto_pos_pending !== 1 ? "s" : ""} to approve`}
          </p>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${inv.out_of_stock > 0 ? "text-red-400" : "text-yellow-400"}`}>{inv.at_risk}</p>
          <p className="text-[11px] text-slate-500">
            {inv.out_of_stock > 0 ? `${inv.out_of_stock} out of stock` : "need reorder"}
          </p>
        </div>
      </div>
      <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <p className="text-xs text-slate-500 mb-2">Lowest coverage</p>
          {inv.items.length === 0 ? (
            <p className="text-slate-500 text-sm">Nothing below reorder level.</p>
          ) : (
            <div className="space-y-2">
              {inv.items.map((i) => (
                <div key={i.item_code} className="flex items-center gap-3">
                  <span className="w-28 shrink-0 text-sm text-slate-300 truncate" title={`${i.item_name} (${i.item_code})`}>
                    {i.item_name}
                  </span>
                  <div className="flex-1 h-2 rounded bg-slate-800 overflow-hidden">
                    <div className={`h-full ${coverageColor(i.coverage, i.out_of_stock)}`} style={{ width: `${Math.max(4, Math.min(100, i.coverage))}%` }} />
                  </div>
                  <span className="w-16 text-right text-xs text-slate-400">
                    {i.current_stock}/{i.reorder_level}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
        <div>
          <p className="text-xs text-slate-500 mb-2">Reorder agent · drafted POs</p>
          {inv.auto_pos.length === 0 ? (
            <p className="text-slate-500 text-sm">No POs awaiting approval.</p>
          ) : (
            <div className="space-y-2">
              {inv.auto_pos.map((p) => (
                <div
                  key={p.po_no}
                  className="flex items-center justify-between rounded-lg border border-emerald-500/30 bg-emerald-500/5 px-3 py-1.5 text-sm"
                >
                  <span className="text-slate-300 truncate" title={p.po_no}>{p.item_name}</span>
                  <span className="text-slate-500 shrink-0 ml-2">
                    {p.order_quantity} {p.unit}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
