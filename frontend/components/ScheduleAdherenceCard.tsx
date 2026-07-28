"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

type ChaseRow = {
  plan_no: string;
  machine: string;
  work_order_no: string | null;
  shift_name: string | null;
  plan_date: string | null;
  planned_quantity: number;
  actual_quantity: number;
  shortfall: number;
  attainment_rate: number | null;
  state: string;
};

// Mirrors ai.schedule.build_schedule_summary (GET /schedule-summary).
type ScheduleSummary = {
  days: number;
  total: number;
  met: number;
  on_track: number;
  behind: number;
  missed: number;
  planned_units: number;
  actual_units: number;
  attainment_rate: number | null;
  by_machine: { machine_id: number | null; machine: string; plans: number; met: number; missed: number; behind: number; attainment_rate: number | null }[];
  chase: ChaseRow[];
  daily: { date: string; planned: number; actual: number; attainment_rate: number | null }[];
  today: { plans: number; planned: number; actual: number; attainment_rate: number | null };
};

function dayLabel(iso: string) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { weekday: "narrow" });
}

// The Production Plan view's intelligence layer over the plan CRUD: did we make
// what we planned? Attainment (due units only — honest denominator), today's
// position, the daily planned-vs-actual strip, and the plans to chase (missed
// first, biggest shortfall). Self-contained; hides on error so the CRUD below
// always renders.
export default function ScheduleAdherenceCard() {
  const [s, setS] = useState<ScheduleSummary | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setS(await apiGet<ScheduleSummary>("/schedule-summary"));
    } catch {
      setS(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !s) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5 text-slate-400 text-sm">
        Loading schedule adherence…
      </div>
    );
  }
  if (!s) return null;

  const trouble = s.behind + s.missed;
  const peak = s.daily.reduce((m, d) => Math.max(m, d.planned, d.actual), 0);

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <h3 className="text-lg font-semibold">Schedule adherence — last {s.days} days</h3>
        <div className={`rounded-xl border px-4 py-2 text-sm ${trouble
          ? "border-amber-500/40 bg-amber-500/10 text-amber-300"
          : "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"}`}>
          {s.total === 0
            ? "No production plans in the window."
            : trouble
              ? `${s.missed} missed, ${s.behind} behind of ${s.total} plans.`
              : `All ${s.total} plans met or on track.`}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat
          label="Attainment (due)"
          value={s.attainment_rate === null ? "—" : `${s.attainment_rate}%`}
          sub={`${s.actual_units.toLocaleString()}/${s.planned_units.toLocaleString()} units`}
          bad={s.attainment_rate !== null && s.attainment_rate < 90}
        />
        <Stat label="Met" value={s.met} sub={`${s.on_track} on track`} />
        <Stat label="Missed" value={s.missed} bad={s.missed > 0} sub={`${s.behind} behind`} />
        <Stat
          label="Today"
          value={s.today.attainment_rate === null ? "—" : `${s.today.attainment_rate}%`}
          sub={`${s.today.actual.toLocaleString()}/${s.today.planned.toLocaleString()} units`}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Daily planned vs actual */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Planned vs made — daily</h4>
          {peak === 0 ? (
            <p className="text-slate-500 text-sm">Nothing planned in the window.</p>
          ) : (
            <div className="flex items-end gap-1.5 h-24">
              {s.daily.map((d, i) => {
                const ph = Math.max(3, Math.round((d.planned / peak) * 84));
                const ah = Math.max(3, Math.round((d.actual / peak) * 84));
                return (
                  <div
                    key={d.date}
                    className="flex-1 flex items-end justify-center gap-0.5"
                    title={`${d.date}: planned ${d.planned.toLocaleString()}, made ${d.actual.toLocaleString()}`}
                  >
                    <div className="w-1/2 rounded-t bg-slate-600/60" style={{ height: `${ph}px` }} />
                    <div
                      className={`w-1/2 rounded-t ${d.actual >= d.planned ? "bg-emerald-500/60" : "bg-amber-500/60"}`}
                      style={{ height: `${ah}px` }}
                    />
                  </div>
                );
              })}
            </div>
          )}
          <p className="text-xs text-slate-500 mt-2">grey = planned · colour = made (green when the day hit plan)</p>
        </div>

        {/* Plans to chase */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Chase first (missed, biggest shortfall)</h4>
          {s.chase.length === 0 ? (
            <p className="text-slate-500 text-sm">Nothing missed or behind.</p>
          ) : (
            <ul className="space-y-2">
              {s.chase.slice(0, 6).map((c) => (
                <li key={c.plan_no} className="flex items-center justify-between gap-3 text-sm">
                  <span className="min-w-0 truncate">
                    <span className="font-semibold">{c.plan_no}</span>
                    <span className="text-slate-400"> · {c.machine}{c.shift_name ? ` · ${c.shift_name}` : ""}</span>
                  </span>
                  <span className={`whitespace-nowrap text-xs ${c.state === "missed" ? "text-red-400" : "text-amber-400"}`}>
                    {c.actual_quantity.toLocaleString()}/{c.planned_quantity.toLocaleString()} · short {c.shortfall.toLocaleString()}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {s.by_machine.length > 0 && (
            <p className="text-xs text-slate-500 mt-3">
              Machines: {s.by_machine.slice(0, 4).map((m) =>
                `${m.machine} (${m.attainment_rate === null ? "—" : `${m.attainment_rate}%`}${m.missed ? `, ${m.missed} missed` : ""})`
              ).join(" · ")}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

function Stat({ label, value, sub, bad }: { label: string; value: number | string; sub?: string; bad?: boolean }) {
  return (
    <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
      <p className="text-slate-400 text-xs">{label}</p>
      <h4 className={`text-2xl font-bold mt-1 ${bad ? "text-red-400" : ""}`}>{value}</h4>
      {sub && <p className="text-[11px] text-slate-500 mt-0.5">{sub}</p>}
    </div>
  );
}
