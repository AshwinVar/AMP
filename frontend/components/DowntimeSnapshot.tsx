"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import DowntimeReasonDrawer from "./DowntimeReasonDrawer";
import MachineDetailDrawer from "./MachineDetailDrawer";

// Mirrors the backend downtime summary (ai/downtime.py build_downtime_summary).
// Reasons and machines are ranked by minutes lost, not event count.
type DowntimeSummary = {
  days: number;
  total_events: number;
  total_minutes: number;
  top_reasons: { reason: string; count: number; minutes: number }[];
  by_machine: { machine_id: number; name: string; count: number; minutes: number }[];
  by_line: { line: string; count: number; minutes: number }[];
  daily: { date: string; count: number; minutes: number }[];
};

// Compact "time lost" label: minutes under an hour, else "Xh Ym" / "Xh".
const fmtMins = (m: number) => {
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const r = m % 60;
  return r ? `${h}h ${r}m` : `${h}h`;
};

// SMT and IC each get a consistent accent across the dashboard (sky / violet).
const lineChip = (line: string) =>
  line === "SMT"
    ? "border-sky-500/40 bg-sky-500/10 text-sky-300"
    : line === "IC"
    ? "border-violet-500/40 bg-violet-500/10 text-violet-300"
    : "border-slate-700 bg-slate-800 text-slate-300";

// A glanceable downtime Pareto — top reasons + most-affected machines over the
// last 7 days. Self-contained: fetches its own summary and refreshes, so it
// drops onto any screen without prop-drilling. Renders nothing when clean.
export default function DowntimeSnapshot() {
  const [dt, setDt] = useState<DowntimeSummary | null>(null);
  const [reason, setReason] = useState<string | null>(null);
  const [machine, setMachine] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      setDt(await apiGet<DowntimeSummary>("/downtime-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!dt || dt.total_events === 0) return null;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Downtime · last 7 days</h3>
        <span className="text-xs text-slate-500">
          {fmtMins(dt.total_minutes)} lost · {dt.total_events} event{dt.total_events !== 1 ? "s" : ""}
        </span>
      </div>
      <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <p className="text-xs text-slate-500 mb-2">Top reasons · by time lost</p>
          <div className="space-y-2">
            {dt.top_reasons.map((r) => {
              // Bar reflects minutes lost (the ranking basis). The lead sets full width;
              // a genuinely zero-minute reason renders no bar (true zero, no min floor).
              const lead = dt.top_reasons[0].minutes || 1;
              const pct = Math.round((r.minutes / lead) * 100);
              return (
                <button
                  key={r.reason}
                  type="button"
                  onClick={() => setReason(r.reason)}
                  className="w-full flex items-center gap-3 rounded-lg px-1 py-1 -mx-1 hover:bg-slate-800/60 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
                  title={`${r.reason} — ${fmtMins(r.minutes)} over ${r.count} event${r.count !== 1 ? "s" : ""}, click for detail`}
                >
                  <span className="w-28 shrink-0 text-sm text-slate-300 truncate text-left">{r.reason}</span>
                  <div className="flex-1 h-2 rounded bg-slate-800 overflow-hidden">
                    <div className="h-full bg-red-500/60" style={{ width: `${r.minutes > 0 ? Math.max(4, pct) : 0}%` }} />
                  </div>
                  <span className="w-14 text-right text-xs text-slate-400">{fmtMins(r.minutes)}</span>
                </button>
              );
            })}
          </div>
        </div>
        <div>
          <p className="text-xs text-slate-500 mb-2">Worst machines · by time lost</p>
          <div className="flex flex-wrap gap-2">
            {dt.by_machine.map((m) => (
              <button
                key={m.machine_id}
                type="button"
                onClick={() => setMachine(m.machine_id)}
                className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:border-slate-500 hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
                title={`${m.name} — ${fmtMins(m.minutes)} over ${m.count} event${m.count !== 1 ? "s" : ""}, open machine cockpit`}
              >
                {m.name} <span className="text-slate-500">· {fmtMins(m.minutes)}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
      {dt.by_line.length > 1 && (
        <div className="mt-4 pt-4 border-t border-slate-800/70">
          <p className="text-xs text-slate-500 mb-2">By line</p>
          <div className="flex flex-wrap gap-2">
            {dt.by_line.map((l) => (
              <span
                key={l.line}
                className={`rounded-md border px-2.5 py-1 text-xs font-medium ${lineChip(l.line)}`}
              >
                {l.line} <span className="opacity-70">· {fmtMins(l.minutes)} · {l.count} event{l.count !== 1 ? "s" : ""}</span>
              </span>
            ))}
          </div>
        </div>
      )}
      {reason && <DowntimeReasonDrawer reason={reason} onClose={() => setReason(null)} />}
      {machine != null && (
        <MachineDetailDrawer machineId={machine} onClose={() => setMachine(null)} onChanged={load} />
      )}
    </div>
  );
}
