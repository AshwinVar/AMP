"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend WIP flow (ai/flow.py build_flow_summary).
type Stage = {
  key: string;
  label: string;
  line: string;
  note: string;
  count: number;
  target: number;
  actual: number;
};

type FlowSummary = { total: number; wip: number; finished: number; stages: Stage[] };

function stageColor(key: string) {
  if (key === "SEMI") return "text-amber-300";
  if (key === "FIN") return "text-emerald-300";
  return "text-slate-300"; // RAW
}

function stageBorder(key: string) {
  if (key === "SEMI") return "border-amber-500/40 bg-amber-500/[0.06]";
  if (key === "FIN") return "border-emerald-500/40 bg-emerald-500/[0.06]";
  return "border-slate-600/50 bg-slate-800/40"; // RAW
}

function lineStyle(line: string) {
  if (line === "SMT") return "border-sky-500/40 bg-sky-500/10 text-sky-300";
  if (line === "IC") return "border-violet-500/40 bg-violet-500/10 text-violet-300";
  return "border-slate-600/40 bg-slate-500/10 text-slate-300";
}

// The two-line WIP pipeline, glanceable on the Overview home: where every work
// order sits as it flows RAW -> (SMT) -> SEMI -> (IC) -> FIN. Self-contained —
// fetches its own summary and refreshes. Renders nothing until there's a WO.
export default function FlowSnapshot() {
  const [f, setF] = useState<FlowSummary | null>(null);

  const load = useCallback(async () => {
    try {
      setF(await apiGet<FlowSummary>("/flow-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!f || f.total === 0) return null;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">WIP pipeline · SMT → IC</h3>
          <p className="text-slate-400 text-sm mt-1">
            {f.wip} in progress · {f.finished} finished · {f.total} work order{f.total !== 1 ? "s" : ""}
          </p>
        </div>
      </div>

      <div className="mt-5 flex items-stretch gap-1 overflow-x-auto pb-1">
        {f.stages.map((s, i) => (
          <div key={s.key} className="flex items-stretch gap-1">
            <div className={`min-w-[140px] flex-1 rounded-xl border p-4 ${stageBorder(s.key)}`}>
              <p className={`text-3xl font-bold ${stageColor(s.key)}`}>{s.count}</p>
              <p className="text-sm font-medium mt-1">{s.label}</p>
              <p className="text-[11px] text-slate-500">{s.note}</p>
              {s.target > 0 && (
                <p className="text-[11px] text-slate-500 mt-1">
                  {s.actual.toLocaleString()} / {s.target.toLocaleString()} units
                </p>
              )}
            </div>
            {i < f.stages.length - 1 && (
              <div className="flex flex-col items-center justify-center px-1 shrink-0">
                <span className={`rounded-full px-2 py-0.5 text-[10px] border ${lineStyle(s.line)}`}>{s.line}</span>
                <span className="text-slate-600 text-xl leading-none mt-1">→</span>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
