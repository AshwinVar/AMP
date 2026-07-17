"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import MachineDetailDrawer from "./MachineDetailDrawer";

// Mirrors the backend production summary (ai/production.py build_production_summary).
type ProductionSummary = {
  days: number;
  runs: number;
  total: number;
  good: number;
  rejected: number;
  good_rate: number;
  by_machine: { machine_id: number; name: string; good: number }[];
  by_line: { line: string; good: number; total: number; good_rate: number }[];
  daily: { date: string; count: number }[];
};

function rateColor(r: number) {
  if (r >= 98) return "text-emerald-400";
  if (r >= 95) return "text-yellow-400";
  if (r >= 90) return "text-orange-400";
  return "text-red-400";
}

function lineStyle(line: string) {
  if (line === "SMT") return "border-sky-500/40 bg-sky-500/10 text-sky-300";
  if (line === "IC") return "border-violet-500/40 bg-violet-500/10 text-violet-300";
  return "border-slate-600/40 bg-slate-500/10 text-slate-300";
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
  const [machine, setMachine] = useState<number | null>(null);

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
              <button
                key={m.machine_id}
                type="button"
                onClick={() => setMachine(m.machine_id)}
                className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:border-slate-500 hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
                title={`${m.name} — open machine cockpit`}
              >
                {m.name} <span className="text-slate-500">· {m.good.toLocaleString()}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
      {p.by_line.length > 0 && (
        <div className="mt-5 flex flex-wrap items-center gap-2">
          <span className="text-xs text-slate-500 mr-1">By line:</span>
          {p.by_line.map((l) => (
            <span key={l.line} className={`rounded-lg border px-3 py-1 text-sm ${lineStyle(l.line)}`}>
              {l.line} <span className="text-slate-400">· {l.good.toLocaleString()} good</span>{" "}
              <span className="text-slate-500">({l.good_rate}%)</span>
            </span>
          ))}
        </div>
      )}
      {machine != null && (
        <MachineDetailDrawer machineId={machine} onClose={() => setMachine(null)} onChanged={load} />
      )}
    </div>
  );
}
