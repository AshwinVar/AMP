"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import QualityDefectDrawer from "./QualityDefectDrawer";
import MachineDetailDrawer from "./MachineDetailDrawer";

// Mirrors the backend quality summary (ai/quality.py build_quality_summary).
type QualitySummary = {
  inspections: number;
  inspected: number;
  passed: number;
  failed: number;
  rework: number;
  scrap: number;
  first_pass_yield: number;
  fail_rate: number;
  top_defects: { category: string; count: number }[];
  by_machine: { machine_id: number; name: string; inspected: number; failed: number; fail_rate: number }[];
};

function yieldColor(fpy: number) {
  if (fpy >= 98) return "text-emerald-400";
  if (fpy >= 95) return "text-yellow-400";
  if (fpy >= 90) return "text-orange-400";
  return "text-red-400";
}

// A glanceable quality read-out — first-pass yield, defect Pareto, worst machines.
// Self-contained: fetches its own summary and refreshes, so it drops onto any
// screen without prop-drilling. Renders nothing until there's data.
export default function QualitySnapshot() {
  const [q, setQ] = useState<QualitySummary | null>(null);
  const [defect, setDefect] = useState<string | null>(null);
  const [machine, setMachine] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      setQ(await apiGet<QualitySummary>("/quality-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!q || q.inspections === 0) return null;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Quality snapshot</h3>
          <p className="text-slate-400 text-sm mt-1">
            {q.inspections} inspection{q.inspections !== 1 ? "s" : ""} · {q.inspected} units · {q.fail_rate}% fail rate
          </p>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${yieldColor(q.first_pass_yield)}`}>{q.first_pass_yield}%</p>
          <p className="text-[11px] text-slate-500">first-pass yield</p>
        </div>
      </div>
      <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <p className="text-xs text-slate-500 mb-2">Top defects</p>
          {q.top_defects.length === 0 ? (
            <p className="text-slate-500 text-sm">No defects recorded.</p>
          ) : (
            <div className="space-y-2">
              {q.top_defects.map((d) => {
                const lead = q.top_defects[0].count || 1;
                const pct = Math.round((d.count / lead) * 100);
                return (
                  <button
                    key={d.category}
                    type="button"
                    onClick={() => setDefect(d.category)}
                    className="w-full flex items-center gap-3 rounded-lg px-1 py-1 -mx-1 hover:bg-slate-800/60 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
                    title={`${d.category} — click for detail`}
                  >
                    <span className="w-28 shrink-0 text-sm text-slate-300 truncate text-left">{d.category}</span>
                    <div className="flex-1 h-2 rounded bg-slate-800 overflow-hidden">
                      <div className="h-full bg-orange-500/60" style={{ width: `${Math.max(6, pct)}%` }} />
                    </div>
                    <span className="w-8 text-right text-xs text-slate-400">{d.count}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
        <div>
          <p className="text-xs text-slate-500 mb-2">Worst machines (fail rate)</p>
          {q.by_machine.length === 0 ? (
            <p className="text-slate-500 text-sm">No machine-linked inspections.</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {q.by_machine.map((m) => (
                <button
                  key={m.machine_id}
                  type="button"
                  onClick={() => setMachine(m.machine_id)}
                  className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:border-slate-500 hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
                  title={`${m.name} — open machine cockpit`}
                >
                  {m.name} <span className="text-slate-500">· {m.fail_rate}%</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
      {defect && <QualityDefectDrawer category={defect} onClose={() => setDefect(null)} />}
      {machine != null && (
        <MachineDetailDrawer machineId={machine} onClose={() => setMachine(null)} onChanged={load} />
      )}
    </div>
  );
}
