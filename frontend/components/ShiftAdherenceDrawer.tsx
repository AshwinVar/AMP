"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend shift drill-down (ai/schedule.py build_shift_adherence).
type ByMachine = {
  machine_id: number | null; machine: string; plans: number;
  met: number; on_track: number; behind: number; missed: number;
  planned: number; actual: number; attainment_rate: number; shortfall: number;
};
type ChasePlan = {
  plan_no: string; machine: string; work_order_no: string | null;
  plan_date: string | null; planned_quantity: number; actual_quantity: number;
  shortfall: number; attainment_rate: number;
  state: "behind" | "missed"; days_ago: number | null;
};
type ShiftDetail = {
  found: boolean;
  shift: string;
  days: number;
  rank: number | null;
  shifts: number;
  total: number;
  met: number; on_track: number; behind: number; missed: number;
  planned_units: number; actual_units: number;
  attainment_rate: number; shortfall_units: number;
  plant_attainment_rate: number; vs_plant: number;
  by_machine: ByMachine[];
  worst_machine: ByMachine | null;
  chase: ChasePlan[];
  daily: { date: string; planned: number; actual: number; attainment_rate: number }[];
};

function attainColor(pct: number) {
  if (pct >= 95) return "text-emerald-400";
  if (pct >= 85) return "text-yellow-400";
  if (pct >= 70) return "text-orange-400";
  return "text-red-400";
}

const ageLabel = (c: ChasePlan) =>
  c.days_ago == null ? "no date"
    : c.days_ago === 0 ? "today"
    : c.days_ago === 1 ? "yesterday"
    : `${c.days_ago}d ago`;

// A right-hand drawer that drills into one shift's schedule adherence: how it
// attains against the plant and where it ranks among the shifts, its plan-state
// mix, a daily planned-vs-actual series, which machines inside the shift lose
// the plan, and the plans to chase. Self-fetches from /schedule-shift; closes on
// Escape — same shape as ConnectionDrawer / MachineReliabilityDrawer.
export default function ShiftAdherenceDrawer({
  shift,
  onClose,
}: {
  shift: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<ShiftDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDetail(await apiGet<ShiftDetail>(`/schedule-shift?shift=${encodeURIComponent(shift)}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load shift detail");
    } finally {
      setLoading(false);
    }
  }, [shift]);

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

  const dailyPeak = detail
    ? Math.max(...detail.daily.map((x) => Math.max(x.planned, x.actual)), 1)
    : 1;

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
            <h2 className="text-2xl font-bold">{shift}</h2>
            <p className="text-slate-500 text-sm mt-1">
              Schedule adherence · last {detail?.days ?? 7}d
              {detail?.rank != null ? ` · #${detail.rank} of ${detail.shifts} worst-first` : ""}
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
          <p className="text-slate-400 mt-6">Loading shift detail…</p>
        ) : detail ? (
          !detail.found ? (
            <p className="text-slate-500 text-sm mt-6">
              No production plans on record for {shift} in the last {detail.days} days.
            </p>
          ) : (
            <div className="mt-5 space-y-6">
              {/* Headline: this shift's attainment read against the plant */}
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                  <p className={`text-3xl font-bold ${attainColor(detail.attainment_rate)}`}>
                    {detail.attainment_rate}%
                  </p>
                  <p className="text-xs text-slate-500 mt-1">
                    plan attained · {detail.actual_units}/{detail.planned_units} units
                  </p>
                </div>
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                  <p
                    className={`text-3xl font-bold ${detail.vs_plant >= 0 ? "text-emerald-400" : "text-red-400"}`}
                  >
                    {detail.vs_plant >= 0 ? "+" : ""}
                    {detail.vs_plant}pp
                  </p>
                  <p className="text-xs text-slate-500 mt-1">
                    vs plant ({detail.plant_attainment_rate}%)
                  </p>
                </div>
              </div>

              {/* What the gap costs, in units */}
              <div className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-sm">
                <span className="text-slate-400">Units short of plan</span>
                <span
                  className={`tabular-nums ${detail.shortfall_units > 0 ? "text-amber-400" : "text-emerald-400"}`}
                >
                  {detail.shortfall_units > 0 ? `−${detail.shortfall_units}` : "none"}
                </span>
              </div>

              {/* Plan-state mix */}
              <div className="grid grid-cols-4 gap-2">
                {([
                  { key: "met", label: "Met", cls: "text-emerald-400" },
                  { key: "on_track", label: "On track", cls: "text-slate-300" },
                  { key: "behind", label: "Behind", cls: "text-amber-400" },
                  { key: "missed", label: "Missed", cls: "text-red-400" },
                ] as const).map((s) => (
                  <div
                    key={s.key}
                    className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center"
                  >
                    <p className={`text-xl font-bold ${s.cls}`}>{detail[s.key]}</p>
                    <p className="text-[11px] text-slate-500">{s.label}</p>
                  </div>
                ))}
              </div>

              {/* Daily planned vs actual, this shift only */}
              {detail.daily.some((x) => x.planned > 0 || x.actual > 0) && (
                <div>
                  <p className="text-xs text-slate-500 mb-1.5">
                    Planned vs actual · last {detail.days} days
                  </p>
                  <div className="flex items-end gap-1.5 h-12">
                    {detail.daily.map((x) => (
                      <div
                        key={x.date}
                        className="flex-1 flex items-end gap-0.5"
                        title={`${x.date}: ${x.actual}/${x.planned} (${x.attainment_rate}%)`}
                      >
                        <div
                          className="flex-1 rounded-sm bg-slate-600/60"
                          style={{ height: `${x.planned === 0 ? 0 : Math.max(3, Math.round((x.planned / dailyPeak) * 100))}%` }}
                        />
                        <div
                          className={`flex-1 rounded-sm ${
                            x.planned === 0 ? "bg-slate-700/40"   /* nothing planned that day — not a miss */
                            : x.attainment_rate >= 95 ? "bg-emerald-500/70"
                            : x.attainment_rate >= 70 ? "bg-amber-400/70" : "bg-red-500/70"}`}
                          style={{ height: `${x.actual === 0 ? 0 : Math.max(3, Math.round((x.actual / dailyPeak) * 100))}%` }}
                        />
                      </div>
                    ))}
                  </div>
                  <div className="mt-1 flex gap-3 text-[10px] text-slate-500">
                    <span className="flex items-center gap-1">
                      <span className="inline-block h-2 w-2 rounded-sm bg-slate-600/60" /> planned
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="inline-block h-2 w-2 rounded-sm bg-emerald-500/70" /> actual
                    </span>
                  </div>
                </div>
              )}

              {/* Where inside the shift the plan is lost */}
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                  Machines on this shift · {detail.by_machine.length}
                </h3>
                {detail.worst_machine && detail.worst_machine.shortfall > 0 && (
                  <p className="text-sm text-slate-400 mt-2">
                    {detail.worst_machine.machine} loses the most plan on this shift —{" "}
                    <span className="text-amber-400">{detail.worst_machine.shortfall} units</span> short
                    across {detail.worst_machine.plans} plan
                    {detail.worst_machine.plans !== 1 ? "s" : ""}.
                  </p>
                )}
                <div className="mt-3 space-y-2">
                  {detail.by_machine.map((m) => (
                    <div
                      key={`${m.machine_id}-${m.machine}`}
                      className="rounded-lg border border-slate-800 px-3 py-2"
                    >
                      <div className="flex items-center justify-between text-sm">
                        <span className="text-slate-200 font-medium">{m.machine}</span>
                        <span className={`tabular-nums ${attainColor(m.attainment_rate)}`}>
                          {m.attainment_rate}%
                        </span>
                      </div>
                      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-slate-500">
                        <span>
                          {m.plans} plan{m.plans !== 1 ? "s" : ""}
                        </span>
                        {m.met > 0 && <span className="text-emerald-400/80">{m.met} met</span>}
                        {m.behind > 0 && <span className="text-amber-400/80">{m.behind} behind</span>}
                        {m.missed > 0 && <span className="text-red-400/80">{m.missed} missed</span>}
                        {m.shortfall > 0 && <span className="text-slate-400">−{m.shortfall} units</span>}
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* The plans to chase on this shift */}
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Plans to chase</h3>
                {detail.chase.length === 0 ? (
                  <p className="text-emerald-400 text-sm mt-3">Nothing behind — this shift is on schedule.</p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {detail.chase.map((c) => (
                      <div
                        key={c.plan_no}
                        className={`flex items-start gap-3 rounded-lg border border-slate-800 border-l-2 ${c.state === "missed" ? "border-l-red-500/70" : "border-l-amber-400/70"} bg-slate-900/40 px-3 py-2`}
                      >
                        <span
                          className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${c.state === "missed" ? "bg-red-500" : "bg-amber-400"}`}
                        />
                        <div className="min-w-0 flex-1">
                          <p className="text-sm text-slate-200 truncate">
                            {c.plan_no} · <span className="text-slate-400">{c.machine}</span>
                          </p>
                          <p className="text-[11px] text-slate-500 truncate">
                            {c.actual_quantity}/{c.planned_quantity} units
                            {c.work_order_no ? ` · ${c.work_order_no}` : ""} · {ageLabel(c)}
                          </p>
                        </div>
                        <span
                          className={`shrink-0 text-[11px] ${c.state === "missed" ? "text-red-400" : "text-amber-400"}`}
                        >
                          −{c.shortfall}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )
        ) : null}
      </div>
    </div>
  );
}
