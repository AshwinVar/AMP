"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import MachineReliabilityDrawer from "./MachineReliabilityDrawer";

// Mirrors the backend reliability read-model (ai/reliability.py build_reliability_summary).
type ByMachine = {
  machine_id: number; name: string; line: string;
  failures: number; repair_minutes: number;
  mttr_minutes: number; mtbf_hours: number | null; availability: number;
};
type Mode = { reason: string; count: number; minutes: number };
type ReliabilitySummary = {
  days: number;
  machines_tracked: number;
  total_failures: number;
  total_repair_minutes: number;
  mttr_minutes: number;
  mtbf_hours: number | null;
  availability: number;
  by_machine: ByMachine[];
  bottleneck: ByMachine | null;
  top_modes: Mode[];
};

function availColor(pct: number) {
  if (pct >= 99) return "text-emerald-400";
  if (pct >= 97) return "text-yellow-400";
  if (pct >= 94) return "text-orange-400";
  return "text-red-400";
}

const mtbf = (h: number | null) => (h == null ? "—" : h >= 24 ? `${(h / 24).toFixed(1)}d` : `${h}h`);
const mttr = (m: number) => (m >= 60 ? `${(m / 60).toFixed(1)}h` : `${Math.round(m)}m`);

// A glanceable reliability read-out — fleet availability, MTBF and MTTR, the
// least-reliable machines, the reliability bottleneck, and where the repair
// hours go. Self-contained: fetches its own summary and refreshes, so it drops
// onto any screen. Renders nothing until there are machines to track.
export default function ReliabilitySnapshot({ onOpen: _onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [d, setD] = useState<ReliabilitySummary | null>(null);
  const [selected, setSelected] = useState<{ id: number; name: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setD(await apiGet<ReliabilitySummary>("/reliability-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!d || d.machines_tracked === 0) return null;

  const modePeak = Math.max(...d.top_modes.map((m) => m.minutes), 1);

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Machine reliability · {d.days} days</h3>
          <p className="text-slate-400 text-sm mt-1">
            {d.machines_tracked} machine{d.machines_tracked !== 1 ? "s" : ""} · {d.total_failures} failure{d.total_failures !== 1 ? "s" : ""}
          </p>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${availColor(d.availability)}`}>{d.availability}%</p>
          <p className="text-[11px] text-slate-500">availability</p>
        </div>
      </div>

      {/* headline KPIs */}
      <div className="mt-4 grid grid-cols-3 gap-2">
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className="text-xl font-bold text-sky-300">{mtbf(d.mtbf_hours)}</p>
          <p className="text-[11px] text-slate-500">MTBF</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className="text-xl font-bold text-amber-300">{d.total_failures ? mttr(d.mttr_minutes) : "—"}</p>
          <p className="text-[11px] text-slate-500">MTTR</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className="text-xl font-bold text-slate-200">{d.total_failures}</p>
          <p className="text-[11px] text-slate-500">failures</p>
        </div>
      </div>

      {d.bottleneck && (
        <div className="mt-4 rounded-lg border border-slate-800 border-l-2 border-l-red-500/70 bg-slate-900/40 px-3 py-2">
          <p className="text-[11px] uppercase tracking-wide text-slate-500">Reliability bottleneck</p>
          <p className="text-sm text-slate-200 mt-0.5">
            <span className="font-medium">{d.bottleneck.name}</span>
            <span className="text-slate-500"> · {d.bottleneck.failures} failure{d.bottleneck.failures !== 1 ? "s" : ""} · MTBF {mtbf(d.bottleneck.mtbf_hours)} · MTTR {mttr(d.bottleneck.mttr_minutes)}</span>
          </p>
        </div>
      )}

      <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* least reliable machines */}
        <div>
          <p className="text-xs text-slate-500 mb-2">Least reliable</p>
          {d.total_failures === 0 ? (
            <p className="text-emerald-400 text-sm">No failures in {d.days} days — fleet running clean.</p>
          ) : (
            <div className="space-y-2">
              {d.by_machine.filter((m) => m.failures > 0).map((m) => {
                const cls = "flex items-center justify-between rounded-lg border border-slate-800 px-3 py-2 text-sm";
                const inner = (
                  <>
                    <div className="min-w-0 flex-1 text-left">
                      <p className="text-slate-200 font-medium truncate">{m.name}</p>
                      <p className="text-[11px] text-slate-500">
                        {m.failures} failure{m.failures !== 1 ? "s" : ""} · MTBF {mtbf(m.mtbf_hours)} · MTTR {mttr(m.mttr_minutes)}
                      </p>
                    </div>
                    <span className={`tabular-nums shrink-0 ${availColor(m.availability)}`}>{m.availability}%</span>
                  </>
                );
                return (
                  <button
                    key={m.machine_id}
                    type="button"
                    onClick={() => setSelected({ id: m.machine_id, name: m.name })}
                    title="Open reliability drill-down"
                    className={`${cls} w-full hover:border-slate-600 hover:bg-slate-800/60 transition focus:outline-none focus:ring-2 focus:ring-slate-600`}
                  >
                    {inner}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* failure modes by repair time */}
        <div>
          <p className="text-xs text-slate-500 mb-2">Repair time by failure mode</p>
          {d.top_modes.length === 0 ? (
            <p className="text-slate-500 text-sm">No stoppages recorded.</p>
          ) : (
            <div className="space-y-2">
              {d.top_modes.map((m) => (
                <div key={m.reason}>
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-slate-200 truncate">{m.reason}</span>
                    <span className="tabular-nums text-slate-400 shrink-0">{mttr(m.minutes)} · {m.count}×</span>
                  </div>
                  <div className="mt-1 h-1.5 rounded-full bg-slate-800">
                    <div
                      className="h-1.5 rounded-full bg-amber-500/70"
                      style={{ width: `${Math.max(4, Math.round((m.minutes / modePeak) * 100))}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
