"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { money } from "../lib/money";

// Mirrors ai.cost.build_cost_summary (GET /cost-summary).
type CostSummary = {
  has_data: boolean;
  days: number;
  loss_cost: number;
  downtime_cost: number;
  scrap_cost: number;
  downtime_minutes: number;
  rejected_units: number;
  biggest: string | null;
  by_line: { line: string; downtime_cost: number; scrap_cost: number; cost: number }[];
  by_machine: { machine_id: number; name: string; downtime_cost: number; scrap_cost: number; cost: number }[];
  daily: { date: string; cost: number }[];
};

// The shared week-over-week verdict trio (GET /cost-trend).
type CostTrend = { direction?: string; verdict?: string; tone?: string };

function toneClasses(tone?: string) {
  switch (tone) {
    case "good": return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
    case "warn": return "border-amber-500/40 bg-amber-500/10 text-amber-300";
    case "bad": return "border-red-500/40 bg-red-500/10 text-red-300";
    default: return "border-slate-800 bg-slate-900 text-slate-300";
  }
}

// The Costing view's intelligence layer over the cost-record CRUD: what the
// week's LOSSES cost (downtime + scrap, the modelled money story), which lever
// is biggest, the machines burning the money, and the week-over-week verdict.
// Self-contained; hides on error so the CRUD below always renders.
export default function CostIntelCard() {
  const [s, setS] = useState<CostSummary | null>(null);
  const [t, setT] = useState<CostTrend | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const [rs, rt] = await Promise.allSettled([
      apiGet<CostSummary>("/cost-summary"),
      apiGet<CostTrend>("/cost-trend"),
    ]);
    setS(rs.status === "fulfilled" ? rs.value : null);
    setT(rt.status === "fulfilled" ? rt.value : null);
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !s) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5 text-slate-400 text-sm">
        Loading cost intelligence…
      </div>
    );
  }
  if (!s || !s.has_data) return null;

  const topCost = s.by_machine[0]?.cost || 1;

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <h3 className="text-lg font-semibold">Cost of losses — last {s.days} days</h3>
        {t?.verdict && (
          <div className={`rounded-xl border px-4 py-2 text-sm ${toneClasses(t.tone)}`}>{t.verdict}</div>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Total lost" value={money(s.loss_cost)} bad={s.loss_cost > 0} />
        <Stat label="Downtime" value={money(s.downtime_cost)} sub={`${s.downtime_minutes.toLocaleString()} min`} />
        <Stat label="Scrap" value={money(s.scrap_cost)} sub={`${s.rejected_units.toLocaleString()} units`} />
        <Stat label="Biggest lever" value={s.biggest || "—"} small />
      </div>

      <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
        <h4 className="text-sm font-semibold text-slate-300 mb-3">Machines burning the money</h4>
        {s.by_machine.length === 0 ? (
          <p className="text-slate-500 text-sm">No losses attributed to machines.</p>
        ) : (
          <ul className="space-y-2">
            {s.by_machine.slice(0, 6).map((m) => (
              <li key={m.machine_id} className="text-sm">
                <div className="flex items-center justify-between gap-3">
                  <span className="min-w-0 truncate font-semibold">{m.name}</span>
                  <span className="text-xs text-slate-400 whitespace-nowrap">
                    ${m.cost.toLocaleString()} (downtime ${m.downtime_cost.toLocaleString()} · scrap ${m.scrap_cost.toLocaleString()})
                  </span>
                </div>
                <div className="mt-1 h-1.5 rounded bg-slate-800">
                  <div
                    className="h-1.5 rounded bg-rose-500/60"
                    style={{ width: `${Math.max(4, Math.round((m.cost / topCost) * 100))}%` }}
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
        {s.by_line.length > 0 && (
          <p className="text-xs text-slate-500 mt-3">
            By line: {s.by_line.map((l) => `${l.line}: ${money(l.cost)}`).join(" · ")}
          </p>
        )}
      </div>
    </section>
  );
}

function Stat({ label, value, sub, bad, small }: { label: string; value: number | string; sub?: string; bad?: boolean; small?: boolean }) {
  return (
    <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
      <p className="text-slate-400 text-xs">{label}</p>
      <h4 className={`${small ? "text-base" : "text-2xl"} font-bold mt-1 truncate ${bad ? "text-rose-400" : ""}`}>{value}</h4>
      {sub && <p className="text-[11px] text-slate-500 mt-0.5">{sub}</p>}
    </div>
  );
}
