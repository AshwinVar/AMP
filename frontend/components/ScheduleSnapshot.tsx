"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend schedule read-model (ai/schedule.py build_schedule_adherence).
type ByShift = {
  shift: string; plans: number;
  met: number; on_track: number; behind: number; missed: number;
  planned: number; actual: number; attainment_rate: number;
};
type ByMachine = {
  machine_id: number | null; machine: string; plans: number;
  met: number; on_track: number; behind: number; missed: number;
  planned: number; actual: number; attainment_rate: number;
};
type ChasePlan = {
  plan_no: string; machine: string; work_order_no: string | null;
  shift_name: string | null; plan_date: string | null;
  planned_quantity: number; actual_quantity: number; shortfall: number;
  attainment_rate: number; state: "behind" | "missed"; days_ago: number | null;
};
type ScheduleSummary = {
  days: number;
  total: number;
  met: number; on_track: number; behind: number; missed: number;
  planned_units: number; actual_units: number; attainment_rate: number;
  by_shift: ByShift[];
  by_machine: ByMachine[];
  chase: ChasePlan[];
  daily: { date: string; planned: number; actual: number; attainment_rate: number }[];
  today: { plans: number; planned: number; actual: number; attainment_rate: number };
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

// A glanceable production-schedule adherence card — the pooled attainment rate,
// the plan-state mix, a daily planned-vs-actual series, a per-shift and
// per-machine split, and the plans to chase. Self-contained: fetches its own
// summary and refreshes, so it drops onto any screen. Renders nothing until
// there are production plans.
export default function ScheduleSnapshot({ onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [d, setD] = useState<ScheduleSummary | null>(null);

  const load = useCallback(async () => {
    try {
      setD(await apiGet<ScheduleSummary>("/schedule-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!d || d.total === 0) return null;

  const dailyPeak = Math.max(...d.daily.map((x) => Math.max(x.planned, x.actual)), 1);

  const states: { key: keyof ScheduleSummary; label: string; cls: string }[] = [
    { key: "met", label: "Met", cls: "text-emerald-400" },
    { key: "on_track", label: "On track", cls: "text-slate-300" },
    { key: "behind", label: "Behind", cls: "text-amber-400" },
    { key: "missed", label: "Missed", cls: "text-red-400" },
  ];

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Schedule · adherence</h3>
          <p className="text-slate-400 text-sm mt-1">
            {d.total} plan{d.total !== 1 ? "s" : ""} · last {d.days}d · {d.behind + d.missed} behind
          </p>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${attainColor(d.attainment_rate)}`}>{d.attainment_rate}%</p>
          <p className="text-[11px] text-slate-500">plan attained</p>
        </div>
      </div>

      {/* state mix */}
      <div className="mt-4 grid grid-cols-4 gap-2">
        {states.map((s) => (
          <div key={s.key} className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
            <p className={`text-xl font-bold ${s.cls}`}>{d[s.key] as number}</p>
            <p className="text-[11px] text-slate-500">{s.label}</p>
          </div>
        ))}
      </div>

      {/* today's scheduled load */}
      <div className="mt-4 flex items-center justify-between rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-sm">
        <span className="text-slate-400">
          Today · {d.today.plans} plan{d.today.plans !== 1 ? "s" : ""}
        </span>
        <span className="text-slate-300 tabular-nums">
          {d.today.actual}/{d.today.planned} units
          <span className={`ml-2 ${attainColor(d.today.attainment_rate)}`}>{d.today.attainment_rate}%</span>
        </span>
      </div>

      {d.daily.some((x) => x.planned > 0 || x.actual > 0) && (
        <div className="mt-4">
          <p className="text-xs text-slate-500 mb-1.5">Planned vs actual · last {d.days} days</p>
          <div className="flex items-end gap-1.5 h-12">
            {d.daily.map((x) => (
              <div key={x.date} className="flex-1 flex items-end gap-0.5" title={`${x.date}: ${x.actual}/${x.planned} (${x.attainment_rate}%)`}>
                <div
                  className="flex-1 rounded-sm bg-slate-600/60"
                  style={{ height: `${Math.max(3, Math.round((x.planned / dailyPeak) * 100))}%` }}
                />
                <div
                  className={`flex-1 rounded-sm ${x.attainment_rate >= 95 ? "bg-emerald-500/70" : x.attainment_rate >= 70 ? "bg-amber-400/70" : "bg-red-500/70"}`}
                  style={{ height: `${Math.max(3, Math.round((x.actual / dailyPeak) * 100))}%` }}
                />
              </div>
            ))}
          </div>
          <div className="mt-1 flex gap-3 text-[10px] text-slate-500">
            <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-sm bg-slate-600/60" /> planned</span>
            <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-sm bg-emerald-500/70" /> actual</span>
          </div>
        </div>
      )}

      <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* by machine */}
        <div>
          <p className="text-xs text-slate-500 mb-2">By machine</p>
          <div className="space-y-2">
            {d.by_machine.map((m) => (
              <div key={`${m.machine_id}-${m.machine}`} className="rounded-lg border border-slate-800 px-3 py-2">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-slate-200 font-medium">{m.machine}</span>
                  <span className={`tabular-nums ${attainColor(m.attainment_rate)}`}>{m.attainment_rate}%</span>
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-slate-500">
                  <span>{m.plans} plan{m.plans !== 1 ? "s" : ""}</span>
                  {m.met > 0 && <span className="text-emerald-400/80">{m.met} met</span>}
                  {m.behind > 0 && <span className="text-amber-400/80">{m.behind} behind</span>}
                  {m.missed > 0 && <span className="text-red-400/80">{m.missed} missed</span>}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* chase list */}
        <div>
          <p className="text-xs text-slate-500 mb-2">Plans to chase</p>
          {d.chase.length === 0 ? (
            <p className="text-emerald-400 text-sm">Nothing behind — on schedule.</p>
          ) : (
            <div className="space-y-2">
              {d.chase.map((c) => {
                const cls = `flex items-start gap-3 rounded-lg border border-slate-800 border-l-2 ${c.state === "missed" ? "border-l-red-500/70" : "border-l-amber-400/70"} bg-slate-900/40 px-3 py-2`;
                const inner = (
                  <>
                    <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${c.state === "missed" ? "bg-red-500" : "bg-amber-400"}`} />
                    <div className="min-w-0 flex-1 text-left">
                      <p className="text-sm text-slate-200 truncate">
                        {c.plan_no} · <span className="text-slate-400">{c.machine}</span>
                      </p>
                      <p className="text-[11px] text-slate-500 truncate">
                        {c.actual_quantity}/{c.planned_quantity} units
                        {c.shift_name ? ` · ${c.shift_name}` : ""} · {ageLabel(c)}
                      </p>
                    </div>
                    <span className={`shrink-0 text-[11px] ${c.state === "missed" ? "text-red-400" : "text-amber-400"}`}>
                      −{c.shortfall}
                    </span>
                  </>
                );
                return onOpen ? (
                  <button
                    key={c.plan_no}
                    type="button"
                    onClick={() => onOpen("planning")}
                    title="Open in Production Planning"
                    className={`${cls} w-full hover:border-slate-600 hover:bg-slate-800/60 transition focus:outline-none focus:ring-2 focus:ring-slate-600`}
                  >
                    {inner}
                  </button>
                ) : (
                  <div key={c.plan_no} className={cls}>{inner}</div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
