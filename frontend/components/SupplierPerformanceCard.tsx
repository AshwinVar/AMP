"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

type SupplierRow = {
  supplier_id: number | null;
  supplier: string;
  orders: number;
  ordered_units: number;
  filled_units: number;
  fill_rate: number | null;
  open: number;
  overdue: number;
  fulfilled: number;
  timed_deliveries: number;
  untimed_deliveries: number;
  on_time: number;
  late: number;
  otd_rate: number | null;
  avg_days_late: number;
};

type OverduePo = {
  po_no: string;
  supplier: string;
  item_name: string;
  ordered: number;
  received: number;
  unit: string;
  expected: string;
  days_overdue: number;
};

// Mirrors ai.supplier_performance.build_supplier_performance (GET /supplier-performance).
type SupplierPerformance = {
  days: number;
  otd_target: number;
  orders_considered: number;
  suppliers: number;
  overdue: number;
  timed_deliveries: number;
  plant_otd: number | null;
  by_supplier: SupplierRow[];
  overdue_pos: OverduePo[];
  verdict: string;
  tone: string;
};

function toneClasses(tone?: string) {
  switch (tone) {
    case "good": return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
    case "warn": return "border-amber-500/40 bg-amber-500/10 text-amber-300";
    case "bad": return "border-red-500/40 bg-red-500/10 text-red-300";
    default: return "border-slate-800 bg-slate-900 text-slate-300";
  }
}

// The buyer's supplier scorecard for the Purchasing view: who delivers on time
// and in full over the last 90 days, and the overdue supply the plant is waiting
// on right now. Self-contained; hides on error so the CRUD below always renders.
export default function SupplierPerformanceCard() {
  const [d, setD] = useState<SupplierPerformance | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setD(await apiGet<SupplierPerformance>("/supplier-performance"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load supplier performance");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !d) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5 text-slate-400 text-sm">
        Loading supplier performance…
      </div>
    );
  }
  if (error || !d) return null;

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <h3 className="text-lg font-semibold">Supplier performance — last {d.days} days</h3>
        <div className={`rounded-xl border px-4 py-2 text-sm ${toneClasses(d.tone)}`}>{d.verdict}</div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Orders" value={d.orders_considered} />
        <Stat
          label={`On-time (target ${d.otd_target}%)`}
          value={d.plant_otd === null ? "—" : `${d.plant_otd}%`}
          bad={d.plant_otd !== null && d.plant_otd < d.otd_target}
        />
        <Stat label="Overdue POs" value={d.overdue} bad={d.overdue > 0} />
        <Stat label="Timed receipts" value={d.timed_deliveries} />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {/* Per-supplier scorecard, worst first */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4 overflow-x-auto">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">By supplier (worst first)</h4>
          {d.by_supplier.length === 0 ? (
            <p className="text-slate-500 text-sm">No purchase orders in the window.</p>
          ) : (
            <table className="w-full min-w-[420px] text-left text-sm">
              <thead className="text-slate-500 text-xs border-b border-slate-800">
                <tr>
                  <th className="py-2 pr-3">Supplier</th>
                  <th className="py-2 pr-3">Fill</th>
                  <th className="py-2 pr-3">On-time</th>
                  <th className="py-2 pr-3">Overdue</th>
                  <th className="py-2">Avg late</th>
                </tr>
              </thead>
              <tbody>
                {d.by_supplier.map((s) => (
                  <tr key={`${s.supplier_id}`} className="border-b border-slate-800/60">
                    <td className="py-2 pr-3 font-semibold">{s.supplier}</td>
                    <td className="py-2 pr-3">{s.fill_rate === null ? "—" : `${s.fill_rate}%`}</td>
                    <td className="py-2 pr-3">
                      {s.otd_rate === null
                        ? <span className="text-slate-500" title={`${s.untimed_deliveries} delivery(ies) had no GRN reference to time`}>not timed</span>
                        : <span className={s.otd_rate < d.otd_target ? "text-amber-400" : "text-emerald-400"}>{s.otd_rate}%</span>}
                    </td>
                    <td className={`py-2 pr-3 ${s.overdue ? "text-red-400 font-semibold" : ""}`}>{s.overdue}</td>
                    <td className="py-2">{s.late ? `${s.avg_days_late}d` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* The supply the plant is waiting on */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Overdue supply — chase now</h4>
          {d.overdue_pos.length === 0 ? (
            <p className="text-slate-500 text-sm">Nothing overdue — supply is on schedule.</p>
          ) : (
            <ul className="space-y-2">
              {d.overdue_pos.slice(0, 6).map((p) => (
                <li key={p.po_no} className="flex items-center justify-between gap-3 text-sm">
                  <span className="min-w-0 truncate">
                    <span className="font-semibold">{p.po_no}</span>
                    <span className="text-slate-400"> · {p.supplier} · {p.item_name}</span>
                  </span>
                  <span className="whitespace-nowrap text-xs text-red-400">
                    {p.received}/{p.ordered} {p.unit} · {p.days_overdue}d late
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}

function Stat({ label, value, bad }: { label: string; value: number | string; bad?: boolean }) {
  return (
    <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
      <p className="text-slate-400 text-xs">{label}</p>
      <h4 className={`text-2xl font-bold mt-1 ${bad ? "text-red-400" : ""}`}>{value}</h4>
    </div>
  );
}
