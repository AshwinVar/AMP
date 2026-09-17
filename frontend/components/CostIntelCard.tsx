"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { lossFigure } from "../lib/money";
import type { CostSummary as Summary } from "./CostSnapshot";

// Mirrors ai.cost.build_cost_summary (GET /cost-summary). Money only at the
// tenant's own unit value; good units otherwise (ADR-0010).
type CostSummary = Summary & {
  days: number;
  downtime_cost: number | null;
  scrap_cost: number | null;
  downtime_minutes: number;
  rejected_units: number;
  downtime_lost_units: number | null;
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
// week's LOSSES cost (downtime + scrap), which lever is biggest, the machines
// losing the most, and the week-over-week verdict. Self-contained; hides on
// error so the CRUD below always renders.
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

  // Bars scale by lost units, which exist with or without a unit value.
  const topUnits = s.by_machine[0]?.lost_units || 1;

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <div>
          <h3 className="text-lg font-semibold">Cost of losses — last {s.days} days</h3>
          {!s.priced && (
            <p className="text-xs text-slate-500 mt-0.5">In good units not made. Set a unit value to see money.</p>
          )}
        </div>
        {t?.verdict && (
          <div className={`rounded-xl border px-4 py-2 text-sm ${toneClasses(t.tone)}`}>{t.verdict}</div>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Total lost" value={lossFigure(s.loss_cost, s.lost_units)} bad={(s.lost_units ?? 0) > 0} />
        <Stat label="Downtime" value={lossFigure(s.downtime_cost, s.downtime_lost_units)} sub={`${s.downtime_minutes.toLocaleString()} min`} />
        <Stat label="Scrap" value={lossFigure(s.scrap_cost, s.rejected_units)} sub={`${s.rejected_units.toLocaleString()} units`} />
        <Stat label="Biggest lever" value={s.biggest || "—"} small />
      </div>

      <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
        <h4 className="text-sm font-semibold text-slate-300 mb-3">Machines losing the most</h4>
        {s.by_machine.length === 0 ? (
          <p className="text-slate-500 text-sm">No losses attributed to machines.</p>
        ) : (
          <ul className="space-y-2">
            {s.by_machine.slice(0, 6).map((m) => (
              <li key={m.machine_id} className="text-sm">
                <div className="flex items-center justify-between gap-3">
                  <span className="min-w-0 truncate font-semibold">{m.name}</span>
                  <span className="text-xs text-slate-400 whitespace-nowrap">
                    {lossFigure(m.cost, m.lost_units)} (downtime {lossFigure(m.downtime_cost, m.downtime_lost_units)} · scrap {lossFigure(m.scrap_cost, m.rejected_units)})
                  </span>
                </div>
                <div className="mt-1 h-1.5 rounded bg-slate-800">
                  <div
                    className="h-1.5 rounded bg-rose-500/60"
                    style={{ width: `${Math.max(4, Math.round(((m.lost_units ?? 0) / topUnits) * 100))}%` }}
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
        {s.by_line.length > 0 && (
          <p className="text-xs text-slate-500 mt-3">
            By line: {s.by_line.map((l) => `${l.line}: ${lossFigure(l.cost, l.lost_units)}`).join(" · ")}
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
