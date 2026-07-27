"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

type CrewRow = {
  operator: string;
  jobs: number;
  completed: number;
  active: number;
  good: number;
  rejected: number;
  units: number;
  quality_rate: number | null;
};

// Mirrors ai.workforce.build_operator_summary (GET /operator-summary).
type OperatorSummary = {
  days: number;
  operators: number;
  jobs: number;
  completed: number;
  active: number;
  good_units: number;
  rejected_units: number;
  total_units: number;
  quality_rate: number | null;
  min_units: number;
  by_operator: CrewRow[];
  needs_attention: CrewRow | null;
};

// Mirrors ai.workforce.build_operator_detail (GET /operator-detail?operator=).
type OperatorDetail = {
  found: boolean;
  operator: string;
  days: number;
  jobs: number;
  completed: number;
  active: number;
  good_units: number;
  rejected_units: number;
  total_units: number;
  quality_rate: number | null;
  plant_quality_rate: number | null;
  vs_plant: number | null;
  rank: number | null;
  operators: number;
  avg_job_minutes: number | null;
  timed_jobs: number;
  by_machine: { machine: string; jobs: number; units: number; quality_rate: number | null }[];
};

function rateClasses(rate: number | null, plant: number | null) {
  if (rate === null) return "text-slate-400";
  if (plant !== null && rate < plant) return "text-amber-400";
  return "text-emerald-400";
}

// The Operator Terminal's performance pair: the crew over the last 7 days (for
// the shift lead) and, when the signed-in user has booked jobs, their own "my
// last 7 days" (for the operator). Fetches independently; hides whatever isn't
// available so the job CRUD below always renders.
export default function OperatorPerformanceCards({ operatorName }: { operatorName?: string }) {
  const [crew, setCrew] = useState<OperatorSummary | null>(null);
  const [me, setMe] = useState<OperatorDetail | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const calls: Promise<unknown>[] = [apiGet<OperatorSummary>("/operator-summary")];
    if (operatorName) {
      calls.push(apiGet<OperatorDetail>(`/operator-detail?operator=${encodeURIComponent(operatorName)}`));
    }
    const rs = await Promise.allSettled(calls);
    setCrew(rs[0].status === "fulfilled" ? (rs[0].value as OperatorSummary) : null);
    const detail = rs[1] && rs[1].status === "fulfilled" ? (rs[1].value as OperatorDetail) : null;
    // Only show "my" numbers when this login actually booked jobs in the window —
    // an admin or a name that never appears in the log just doesn't get the card.
    setMe(detail && detail.found ? detail : null);
    setLoading(false);
  }, [operatorName]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !crew && !me) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5 text-slate-400 text-sm">
        Loading operator performance…
      </div>
    );
  }
  if (!crew && !me) return null;

  return (
    <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
      {me && (
        <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
          <h3 className="text-lg font-semibold">My last {me.days} days</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Stat label="Jobs" value={`${me.completed}/${me.jobs}`} sub="completed" />
            <Stat label="Units booked" value={me.total_units.toLocaleString()} sub={`${me.good_units.toLocaleString()} good`} />
            <Stat
              label="Quality"
              value={me.quality_rate === null ? "—" : `${me.quality_rate}%`}
              sub={me.vs_plant === null ? "no plant baseline" : `${me.vs_plant >= 0 ? "+" : ""}${me.vs_plant} pts vs plant`}
              tone={rateClasses(me.quality_rate, me.plant_quality_rate)}
            />
            <Stat
              label="Crew rank"
              value={me.rank === null ? "—" : `${me.rank}/${me.operators}`}
              sub={me.avg_job_minutes === null ? "no timed jobs" : `avg job ${me.avg_job_minutes}m`}
            />
          </div>
          {me.by_machine.length > 0 && (
            <p className="text-xs text-slate-500">
              Machines: {me.by_machine.slice(0, 4).map((m) =>
                `${m.machine} (${m.units.toLocaleString()}u${m.quality_rate !== null ? `, ${m.quality_rate}%` : ""})`
              ).join(" · ")}
            </p>
          )}
        </section>
      )}

      {crew && (
        <section className={`rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4 ${!me ? "xl:col-span-2" : ""}`}>
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-lg font-semibold">Crew — last {crew.days} days</h3>
            <span className="text-xs text-slate-400">
              {crew.operators} operator{crew.operators === 1 ? "" : "s"} · {crew.jobs} jobs ·{" "}
              {crew.quality_rate === null ? "no units booked" : `${crew.quality_rate}% quality`}
            </span>
          </div>
          {crew.by_operator.length === 0 ? (
            <p className="text-slate-500 text-sm">No operator jobs in the window.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[420px] text-left text-sm">
                <thead className="text-slate-500 text-xs border-b border-slate-800">
                  <tr>
                    <th className="py-2 pr-3">Operator</th>
                    <th className="py-2 pr-3">Jobs</th>
                    <th className="py-2 pr-3">Units</th>
                    <th className="py-2">Quality</th>
                  </tr>
                </thead>
                <tbody>
                  {crew.by_operator.map((o) => (
                    <tr key={o.operator} className="border-b border-slate-800/60">
                      <td className="py-2 pr-3 font-semibold">
                        {o.operator}
                        {crew.needs_attention?.operator === o.operator && (
                          <span className="ml-2 text-xs text-amber-400">← look here first</span>
                        )}
                      </td>
                      <td className="py-2 pr-3">{o.completed}/{o.jobs}</td>
                      <td className="py-2 pr-3">{o.units.toLocaleString()}</td>
                      <td className={`py-2 ${rateClasses(o.quality_rate, crew.quality_rate)}`}>
                        {o.quality_rate === null
                          ? <span className="text-slate-500" title={`fewer than ${crew.min_units} units booked`}>—</span>
                          : `${o.quality_rate}%`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: number | string; sub?: string; tone?: string }) {
  return (
    <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
      <p className="text-slate-400 text-xs">{label}</p>
      <h4 className={`text-2xl font-bold mt-1 ${tone || ""}`}>{value}</h4>
      {sub && <p className="text-[11px] text-slate-500 mt-0.5">{sub}</p>}
    </div>
  );
}
