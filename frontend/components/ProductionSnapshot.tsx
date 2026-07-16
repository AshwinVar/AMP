"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend production summary (ai/production.py build_production_summary).
type ProductionSummary = {
  days: number;
  runs: number;
  total: number;
  good: number;
  rejected: number;
  good_rate: number;
  by_machine: { machine_id: number; name: string; good: number }[];
  daily: { date: string; count: number }[];
};

function rateColor(r: number) {
  if (r >= 98) return "text-emerald-400";
  if (r >= 95) return "text-yellow-400";
  if (r >= 90) return "text-orange-400";
  return "text-red-400";
}

function wk(iso: string) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { weekday: "short" });
}

// A glanceable production read-out — throughput sparkline, good rate, top
// producers. Self-contained: fetches its own summary and refreshes, so it drops
// onto any screen without prop-drilling. Renders nothing until there's data.
export default function ProductionSnapshot() {
  const [p, setP] = useState<ProductionSummary | null>(null);

  const load = useCallback(async () => {
    try {
      setP(await apiGet<ProductionSummary>("/production-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!p || p.runs === 0) return null;

  const peak = p.daily.reduce((m, d) => Math.max(m, d.count), 0);

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Production · last 7 days</h3>
          <p className="text-slate-400 text-sm mt-1">
            {p.good.toLocaleString()} good · {p.rejected.toLocaleString()} rejected · {p.runs} run{p.runs !== 1 ? "s" : ""}
          </p>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${rateColor(p.good_rate)}`}>{p.good_rate}%</p>
          <p className="text-[11px] text-slate-500">good rate</p>
        </div>
      </div>
      <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <p className="text-xs text-slate-500 mb-2">Throughput</p>
          <div className="flex items-end gap-2 h-20">
            {p.daily.map((d) => {
              const h = peak ? Math.max(4, Math.round((d.count / peak) * 72)) : 4;
              return (
                <div
                  key={d.date}
                  className="flex-1 flex flex-col items-center justify-end gap-1"
                  title={`${d.count} on ${d.date}`}
                >
                  <div className="w-full bg-emerald-500/60 rounded-t" style={{ height: `${h}px` }} />
                  <span className="text-[10px] text-slate-500">{wk(d.date)}</span>
                </div>
              );
            })}
          </div>
        </div>
        <div>
          <p className="text-xs text-slate-500 mb-2">Top producers</p>
          <div className="flex flex-wrap gap-2">
            {p.by_machine.map((m) => (
              <span key={m.machine_id} className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300">
                {m.name} <span className="text-slate-500">· {m.good.toLocaleString()}</span>
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
