"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend reliability drill-down (ai/reliability.py build_machine_reliability).
type Mode = { reason: string; count: number; minutes: number };
type Day = { date: string; failures: number; minutes: number };
type Task = {
  task_no: string; task_type: string; priority: string;
  status: string; planned_date: string | null; overdue: boolean;
};
type Stoppage = {
  id: number; reason: string; duration: string;
  minutes: number; notes: string | null; at: string | null;
};
type MachineReliability = {
  machine_id: number;
  found: boolean;
  name: string;
  line: string;
  status: string | null;
  days: number;
  failures: number;
  repair_minutes: number;
  mttr_minutes: number;
  mtbf_hours: number | null;
  availability: number;
  top_modes: Mode[];
  daily: Day[];
  open_tasks: Task[];
  open_task_count: number;
  overdue_task_count: number;
  recent: Stoppage[];
};

function availColor(pct: number) {
  if (pct >= 99) return "text-emerald-400";
  if (pct >= 97) return "text-yellow-400";
  if (pct >= 94) return "text-orange-400";
  return "text-red-400";
}

const PRIORITY_CLS: Record<string, string> = {
  Critical: "text-red-400",
  High: "text-orange-400",
  Medium: "text-amber-400",
  Low: "text-slate-400",
};

const mtbf = (h: number | null) => (h == null ? "—" : h >= 24 ? `${(h / 24).toFixed(1)}d` : `${h}h`);
const mttr = (m: number) => (m >= 60 ? `${(m / 60).toFixed(1)}h` : `${Math.round(m)}m`);
const when = (iso: string | null) => (iso ? iso.slice(0, 16).replace("T", " ") : "—");

// A right-hand drawer that drills into one machine's reliability: its 30-day
// availability / MTBF / MTTR, a daily stoppage timeline, the failure modes eating
// its uptime, the open maintenance tasks working the problem, and its recent
// stoppages. Self-fetches from /machine-reliability; closes on Escape.
export default function MachineReliabilityDrawer({
  machineId,
  machineName,
  onClose,
}: {
  machineId: number;
  machineName: string;
  onClose: () => void;
}) {
  const [d, setD] = useState<MachineReliability | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setD(await apiGet<MachineReliability>(`/machine-reliability?machine_id=${machineId}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load machine reliability");
    } finally {
      setLoading(false);
    }
  }, [machineId]);

  useEffect(() => {
    load();
  }, [load]);

  // Close on Escape for keyboard users.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const dayPeak = d ? Math.max(...d.daily.map((x) => x.minutes), 1) : 1;
  const modePeak = d ? Math.max(...d.top_modes.map((m) => m.minutes), 1) : 1;

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} aria-hidden="true" />
      <div
        role="dialog"
        aria-modal="true"
        className="relative w-full max-w-xl bg-slate-950 border-l border-slate-800 h-full overflow-y-auto p-6"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-2xl font-bold">{machineName}</h2>
            <p className="text-slate-500 text-sm mt-1">
              Machine reliability
              {d?.line ? ` · ${d.line}` : ""}
              {d?.status ? ` · ${d.status}` : ""}
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl px-2" aria-label="Close">
            ✕
          </button>
        </div>

        {error && (
          <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-3 text-sm">{error}</div>
        )}

        {loading && !d ? (
          <p className="text-slate-400 mt-6">Loading machine reliability…</p>
        ) : d ? (
          !d.found ? (
            <p className="text-slate-500 text-sm mt-6">No machine on record for #{d.machine_id}.</p>
          ) : (
            <div className="mt-5 space-y-6">
              {/* Headline availability + KPIs */}
              <div className="grid grid-cols-4 gap-2">
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-4 text-center">
                  <p className={`text-2xl font-bold ${availColor(d.availability)}`}>{d.availability}%</p>
                  <p className="text-[11px] text-slate-500 mt-1">availability</p>
                </div>
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-4 text-center">
                  <p className="text-2xl font-bold text-sky-300">{mtbf(d.mtbf_hours)}</p>
                  <p className="text-[11px] text-slate-500 mt-1">MTBF</p>
                </div>
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-4 text-center">
                  <p className="text-2xl font-bold text-amber-300">{d.failures ? mttr(d.mttr_minutes) : "—"}</p>
                  <p className="text-[11px] text-slate-500 mt-1">MTTR</p>
                </div>
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-4 text-center">
                  <p className="text-2xl font-bold text-slate-200">{d.failures}</p>
                  <p className="text-[11px] text-slate-500 mt-1">failures</p>
                </div>
              </div>

              {d.failures === 0 ? (
                <p className="text-emerald-400 text-sm">No stoppages in {d.days} days — running clean.</p>
              ) : (
                <>
                  {/* Daily stoppage timeline */}
                  <div>
                    <p className="text-xs text-slate-500 mb-1.5">
                      Downtime over {d.days} days · {d.repair_minutes} min total
                    </p>
                    <div className="flex items-end gap-px h-12">
                      {d.daily.map((x) => (
                        <div
                          key={x.date}
                          className={`flex-1 rounded-sm ${x.minutes > 0 ? "bg-amber-500/60" : "bg-slate-800/60"}`}
                          style={{ height: `${Math.max(3, Math.round((x.minutes / dayPeak) * 100))}%` }}
                          title={`${x.date}: ${x.failures} stoppage${x.failures !== 1 ? "s" : ""}, ${x.minutes} min`}
                        />
                      ))}
                    </div>
                  </div>

                  {/* Failure modes by repair time */}
                  <div>
                    <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide mb-3">
                      Repair time by failure mode
                    </h3>
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
                  </div>
                </>
              )}

              {/* Open maintenance tasks — what's being done about it */}
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                  Open maintenance
                  {d.open_task_count > 0 ? ` · ${d.open_task_count}` : ""}
                  {d.overdue_task_count > 0 ? (
                    <span className="text-red-400"> · {d.overdue_task_count} overdue</span>
                  ) : null}
                </h3>
                {d.open_tasks.length === 0 ? (
                  <p className="text-slate-500 text-sm mt-3">No open maintenance tasks on this machine.</p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {d.open_tasks.map((t) => (
                      <div
                        key={t.task_no}
                        className={`flex items-start gap-3 rounded-lg border border-slate-800 border-l-2 ${t.overdue ? "border-l-red-500/70" : "border-l-slate-700"} bg-slate-900/40 px-3 py-2`}
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-sm text-slate-200 truncate">
                            {t.task_no} <span className="text-slate-500">· {t.task_type}</span>
                          </p>
                          <p className="text-[11px] text-slate-500 truncate">
                            {t.status}
                            {t.planned_date ? ` · planned ${t.planned_date}` : ""}
                          </p>
                        </div>
                        <span className={`shrink-0 text-[11px] ${t.overdue ? "text-red-400" : PRIORITY_CLS[t.priority] || "text-slate-400"}`}>
                          {t.overdue ? "overdue" : t.priority}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Recent stoppages */}
              {d.recent.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Recent stoppages</h3>
                  <ol className="mt-3 space-y-3">
                    {d.recent.map((s) => (
                      <li key={s.id} className="border-b border-slate-800/70 pb-3">
                        <div className="flex items-center justify-between gap-2">
                          <p className="text-sm font-medium truncate">{s.reason}</p>
                          <span className="text-xs text-amber-400 shrink-0 tabular-nums">{mttr(s.minutes)}</span>
                        </div>
                        <p className="text-xs text-slate-600 mt-0.5">
                          {when(s.at)}
                          {s.notes ? ` · ${s.notes}` : ""}
                        </p>
                      </li>
                    ))}
                  </ol>
                </div>
              )}
            </div>
          )
        ) : null}
      </div>
    </div>
  );
}
