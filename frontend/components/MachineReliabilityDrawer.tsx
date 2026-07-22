"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend machine drill-down (ai/reliability.py build_machine_reliability).
type Mode = { reason: string; count: number; minutes: number };
type Week = { week_start: string; failures: number; minutes: number };
type Failure = { reason: string; minutes: number; notes: string | null; at: string };
type Task = {
  task_no: string; task_type: string; priority: string; status: string;
  planned_date: string | null; overdue: boolean;
};

type MachineReliability = {
  found: boolean;
  machine_id: number;
  name: string | null;
  line: string;
  status: string | null;
  days: number;
  failures: number;
  repair_minutes: number;
  mttr_minutes: number;
  mtbf_hours: number | null;
  availability: number;
  rank: number | null;
  machines_tracked: number;
  fleet_mttr_minutes: number;
  fleet_availability: number;
  top_modes: Mode[];
  weekly: Week[];
  trend: "worsening" | "improving" | "steady";
  recent_failures: number;
  prior_failures: number;
  hours_since_last_failure: number | null;
  overdue_vs_mtbf: boolean;
  failures_log: Failure[];
  maintenance: { open: number; overdue: number; tasks: Task[] };
};

function availColor(pct: number) {
  if (pct >= 99) return "text-emerald-400";
  if (pct >= 97) return "text-yellow-400";
  if (pct >= 94) return "text-orange-400";
  return "text-red-400";
}

const mtbfLabel = (h: number | null) => (h == null ? "—" : h >= 24 ? `${(h / 24).toFixed(1)}d` : `${h}h`);
const minsLabel = (m: number) => (m >= 60 ? `${(m / 60).toFixed(1)}h` : `${Math.round(m)}m`);

const TREND: Record<MachineReliability["trend"], { label: string; cls: string }> = {
  worsening: { label: "worsening", cls: "text-red-400" },
  improving: { label: "improving", cls: "text-emerald-400" },
  steady: { label: "steady", cls: "text-slate-400" },
};

const PRIORITY_CLS: Record<string, string> = {
  Critical: "text-red-400",
  High: "text-orange-400",
  Medium: "text-yellow-400",
  Low: "text-slate-400",
};

// A right-hand drawer that drills into one machine's reliability: its 30-day
// MTBF / MTTR / availability read against the fleet, where it ranks, its own
// failure modes, a weekly failure trend, the failures themselves, and the
// maintenance already booked to fix it. Self-fetches from /reliability-machine;
// closes on Escape.
export default function MachineReliabilityDrawer({
  machineId,
  onClose,
}: {
  machineId: number;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<MachineReliability | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDetail(await apiGet<MachineReliability>(`/reliability-machine?machine_id=${machineId}`));
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

  const weekPeak = detail ? Math.max(...detail.weekly.map((w) => w.failures), 1) : 1;
  const modePeak = detail ? Math.max(...detail.top_modes.map((m) => m.minutes), 1) : 1;

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
            <h2 className="text-2xl font-bold">{detail?.name ?? `Machine #${machineId}`}</h2>
            <p className="text-slate-500 text-sm mt-1">
              Reliability · last {detail?.days ?? 30} days
              {detail?.line ? ` · ${detail.line}` : ""}
              {detail?.status ? ` · ${detail.status}` : ""}
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl px-2" aria-label="Close">
            ✕
          </button>
        </div>

        {error && (
          <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-3 text-sm">{error}</div>
        )}

        {loading && !detail ? (
          <p className="text-slate-400 mt-6">Loading machine reliability…</p>
        ) : detail ? (
          !detail.found ? (
            <p className="text-slate-500 text-sm mt-6">No machine on record for #{machineId}.</p>
          ) : (
            <div className="mt-5 space-y-6">
              {/* Headline: availability + rank against the fleet */}
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                  <p className={`text-3xl font-bold ${availColor(detail.availability)}`}>{detail.availability}%</p>
                  <p className="text-xs text-slate-500 mt-1">
                    availability · fleet {detail.fleet_availability}%
                  </p>
                </div>
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                  <p className="text-3xl font-bold text-slate-200">
                    {detail.rank ? `#${detail.rank}` : "—"}
                  </p>
                  <p className="text-xs text-slate-500 mt-1">
                    least reliable · of {detail.machines_tracked} machine{detail.machines_tracked !== 1 ? "s" : ""}
                  </p>
                </div>
              </div>

              {/* Reliability KPIs */}
              <div className="grid grid-cols-3 gap-2">
                <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
                  <p className="text-xl font-bold text-sky-300">{mtbfLabel(detail.mtbf_hours)}</p>
                  <p className="text-[11px] text-slate-500">MTBF</p>
                </div>
                <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
                  <p className="text-xl font-bold text-amber-300">
                    {detail.failures ? minsLabel(detail.mttr_minutes) : "—"}
                  </p>
                  <p className="text-[11px] text-slate-500">
                    MTTR · fleet {detail.fleet_mttr_minutes ? minsLabel(detail.fleet_mttr_minutes) : "—"}
                  </p>
                </div>
                <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
                  <p className="text-xl font-bold text-slate-200">{detail.failures}</p>
                  <p className="text-[11px] text-slate-500">failures · {minsLabel(detail.repair_minutes)} down</p>
                </div>
              </div>

              {detail.failures === 0 ? (
                <p className="text-emerald-400 text-sm">
                  No failures in {detail.days} days — this machine has been running clean.
                </p>
              ) : (
                <p className="text-sm text-slate-400">
                  Trend <span className={TREND[detail.trend].cls}>{TREND[detail.trend].label}</span> ·{" "}
                  {detail.recent_failures} failure{detail.recent_failures !== 1 ? "s" : ""} in the last{" "}
                  {Math.round(detail.days / 2)} days vs {detail.prior_failures} before
                  {detail.hours_since_last_failure != null
                    ? ` · last stop ${mtbfLabel(detail.hours_since_last_failure)} ago`
                    : ""}
                </p>
              )}

              {detail.overdue_vs_mtbf && (
                <div className="rounded-lg border border-slate-800 border-l-2 border-l-amber-400/70 bg-slate-900/40 px-3 py-2">
                  <p className="text-sm text-amber-300">
                    Running past its own mean interval — it has gone{" "}
                    {mtbfLabel(detail.hours_since_last_failure)} since the last stop against an MTBF of{" "}
                    {mtbfLabel(detail.mtbf_hours)}. Statistically due to stop again.
                  </p>
                </div>
              )}

              {/* Weekly failure trend */}
              {detail.weekly.some((w) => w.failures > 0) && (
                <div>
                  <p className="text-xs text-slate-500 mb-1.5">Failures by week</p>
                  <div className="flex items-end gap-1 h-10">
                    {detail.weekly.map((w) => (
                      <div
                        key={w.week_start}
                        className="flex-1 rounded-sm bg-red-500/60"
                        style={{ height: `${Math.max(4, Math.round((w.failures / weekPeak) * 100))}%` }}
                        title={`Week of ${w.week_start}: ${w.failures} failure(s), ${minsLabel(w.minutes)} down`}
                      />
                    ))}
                  </div>
                </div>
              )}

              {/* Failure modes for this machine */}
              {detail.top_modes.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                    Repair time by failure mode
                  </h3>
                  <div className="mt-3 space-y-2">
                    {detail.top_modes.map((m) => (
                      <div key={m.reason}>
                        <div className="flex items-center justify-between text-sm">
                          <span className="text-slate-200 truncate">{m.reason}</span>
                          <span className="tabular-nums text-slate-400 shrink-0">
                            {minsLabel(m.minutes)} · {m.count}×
                          </span>
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
              )}

              {/* Booked maintenance — what's already scheduled to fix it */}
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                  Booked maintenance · {detail.maintenance.open} open
                  {detail.maintenance.overdue > 0 ? ` · ${detail.maintenance.overdue} overdue` : ""}
                </h3>
                {detail.maintenance.tasks.length === 0 ? (
                  <p className="text-slate-500 text-sm mt-3">
                    {detail.failures > 0
                      ? "Nothing booked against this machine — no maintenance is scheduled to fix it."
                      : "Nothing booked — nothing outstanding."}
                  </p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {detail.maintenance.tasks.map((t) => (
                      <div
                        key={t.task_no}
                        className={`flex items-start gap-3 rounded-lg border border-slate-800 border-l-2 ${t.overdue ? "border-l-red-500/70" : "border-l-slate-700"} bg-slate-900/40 px-3 py-2`}
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-sm text-slate-200 truncate">
                            {t.task_no} <span className="text-slate-500">· {t.task_type}</span>
                          </p>
                          <p className="text-[11px] text-slate-500">
                            <span className={PRIORITY_CLS[t.priority] ?? "text-slate-400"}>{t.priority}</span> ·{" "}
                            {t.status}
                            {t.planned_date ? ` · planned ${t.planned_date}` : ""}
                          </p>
                        </div>
                        {t.overdue && <span className="shrink-0 text-[11px] text-red-400">overdue</span>}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* The failures themselves */}
              {detail.failures_log.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Recent failures</h3>
                  <ol className="mt-3 space-y-3">
                    {detail.failures_log.map((f, i) => (
                      <li key={`${f.at}-${i}`} className="border-b border-slate-800/70 pb-3">
                        <div className="flex items-center justify-between gap-2">
                          <p className="text-sm font-medium truncate">{f.reason}</p>
                          <span className="text-xs text-amber-400 shrink-0">{minsLabel(f.minutes)}</span>
                        </div>
                        <p className="text-xs text-slate-600 mt-0.5">
                          {new Date(f.at).toLocaleString()}
                          {f.notes ? ` · ${f.notes}` : ""}
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
