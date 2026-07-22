"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import SupplierDrawer from "./SupplierDrawer";

// Mirrors the backend supply read-model (ai/supply.py build_supply_summary).
type BySupplier = {
  supplier: string; pos: number;
  received: number; on_track: number; at_risk: number; late: number;
  ordered: number; received_units: number; receipt_rate: number;
};
type ChasePo = {
  po_no: string; supplier: string; item_name: string; expected_delivery_date: string | null;
  order_quantity: number; received_quantity: number; unit: string;
  state: "at_risk" | "late"; days_to_due: number | null;
};
type SupplySummary = {
  total: number;
  received: number; on_track: number; at_risk: number; late: number;
  receipt_rate: number;
  by_supplier: BySupplier[];
  chase: ChasePo[];
  upcoming: { date: string; pos: number }[];
};

function receiptColor(pct: number) {
  if (pct >= 95) return "text-emerald-400";
  if (pct >= 80) return "text-yellow-400";
  if (pct >= 60) return "text-orange-400";
  return "text-red-400";
}

const dueLabel = (o: ChasePo) =>
  o.days_to_due == null ? "no due date"
    : o.state === "late" ? `${Math.abs(o.days_to_due)}d overdue`
    : o.days_to_due === 0 ? "due today"
    : `due in ${o.days_to_due}d`;

// A glanceable inbound-supply outlook — unit receipt rate, the state mix, a
// per-supplier split, and the POs to chase. Self-contained: fetches its own
// summary and refreshes, so it drops onto any screen. Renders nothing until
// there are purchase orders.
export default function SupplySnapshot({ onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [d, setD] = useState<SupplySummary | null>(null);
  const [supplier, setSupplier] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setD(await apiGet<SupplySummary>("/supply-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!d || d.total === 0) return null;

  const upcomingPeak = Math.max(...d.upcoming.map((u) => u.pos), 1);

  const states: { key: keyof SupplySummary; label: string; cls: string }[] = [
    { key: "received", label: "Received", cls: "text-emerald-400" },
    { key: "on_track", label: "On track", cls: "text-slate-300" },
    { key: "at_risk", label: "At risk", cls: "text-amber-400" },
    { key: "late", label: "Late", cls: "text-red-400" },
  ];

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Inbound supply · outlook</h3>
          <p className="text-slate-400 text-sm mt-1">
            {d.total} PO{d.total !== 1 ? "s" : ""} · {d.at_risk + d.late} need attention
          </p>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${receiptColor(d.receipt_rate)}`}>{d.receipt_rate}%</p>
          <p className="text-[11px] text-slate-500">units received</p>
        </div>
      </div>

      {/* state mix */}
      <div className="mt-4 grid grid-cols-4 gap-2">
        {states.map((s) => (
          <div key={s.key} className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
            <p className={`text-xl font-bold ${s.cls}`}>{d[s.key] as number}</p>
            <p className="text-[11px] text-slate-500">{s.label}</p>
          </div>
        ))}
      </div>

      {d.upcoming.some((u) => u.pos > 0) && (
        <div className="mt-4">
          <p className="text-xs text-slate-500 mb-1.5">Expected next 7 days</p>
          <div className="flex items-end gap-1 h-10">
            {d.upcoming.map((u) => (
              <div
                key={u.date}
                className="flex-1 rounded-sm bg-sky-500/60"
                style={{ height: `${u.pos === 0 ? 0 : Math.max(4, Math.round((u.pos / upcomingPeak) * 100))}%` }}
                title={`${u.date}: ${u.pos} due`}
              />
            ))}
          </div>
        </div>
      )}

      <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* by supplier */}
        <div>
          <p className="text-xs text-slate-500 mb-2">By supplier</p>
          <div className="space-y-2">
            {d.by_supplier.map((s) => (
              <button
                key={s.supplier}
                type="button"
                onClick={() => setSupplier(s.supplier)}
                title={`${s.supplier} — click for detail`}
                className="w-full rounded-lg border border-slate-800 px-3 py-2 text-left hover:border-slate-600 hover:bg-slate-800/60 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
              >
                <div className="flex items-center justify-between text-sm">
                  <span className="text-slate-200 font-medium">{s.supplier}</span>
                  <span className={`tabular-nums ${receiptColor(s.receipt_rate)}`}>{s.receipt_rate}%</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-slate-500">
                  <span>{s.pos} PO{s.pos !== 1 ? "s" : ""}</span>
                  {s.received > 0 && <span className="text-emerald-400/80">{s.received} received</span>}
                  {s.at_risk > 0 && <span className="text-amber-400/80">{s.at_risk} at risk</span>}
                  {s.late > 0 && <span className="text-red-400/80">{s.late} late</span>}
                </div>
              </button>
            ))}
          </div>
        </div>

        {/* chase list */}
        <div>
          <p className="text-xs text-slate-500 mb-2">POs to chase</p>
          {d.chase.length === 0 ? (
            <p className="text-emerald-400 text-sm">Nothing at risk — inbound supply on track.</p>
          ) : (
            <div className="space-y-2">
              {d.chase.map((o) => {
                const cls = `flex items-start gap-3 rounded-lg border border-slate-800 border-l-2 ${o.state === "late" ? "border-l-red-500/70" : "border-l-amber-400/70"} bg-slate-900/40 px-3 py-2`;
                const inner = (
                  <>
                    <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${o.state === "late" ? "bg-red-500" : "bg-amber-400"}`} />
                    <div className="min-w-0 flex-1 text-left">
                      <p className="text-sm text-slate-200 truncate">
                        {o.po_no} · <span className="text-slate-400">{o.supplier}</span>
                      </p>
                      <p className="text-[11px] text-slate-500 truncate">
                        {o.item_name} · {o.received_quantity}/{o.order_quantity} {o.unit} in
                      </p>
                    </div>
                    <span className={`shrink-0 text-[11px] ${o.state === "late" ? "text-red-400" : "text-amber-400"}`}>
                      {dueLabel(o)}
                    </span>
                  </>
                );
                return onOpen ? (
                  <button
                    key={o.po_no}
                    type="button"
                    onClick={() => onOpen("inventory")}
                    title="Open in Inventory & Supply"
                    className={`${cls} w-full hover:border-slate-600 hover:bg-slate-800/60 transition focus:outline-none focus:ring-2 focus:ring-slate-600`}
                  >
                    {inner}
                  </button>
                ) : (
                  <div key={o.po_no} className={cls}>{inner}</div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {supplier && <SupplierDrawer supplier={supplier} onClose={() => setSupplier(null)} />}
    </div>
  );
}
