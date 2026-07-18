"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend cost read-model (ai/cost.py build_cost_summary).
type Loss = { key: string; label: string; cost: number; detail: string };
type CostSummary = {
  has_data: boolean;
  loss_cost: number;
  downtime_cost: number;
  scrap_cost: number;
  losses: Loss[];
  biggest: string | null;
  recorded_total: number;
  by_type: { type: string; amount: number }[];
};

const money = (n: number) => `$${n.toLocaleString()}`;

// The cost-of-losses card: what downtime and scrap cost this week, in money,
// plus the costs actually recorded. Self-contained — fetches its own summary and
// refreshes. Renders nothing until there's something to price.
export default function CostSnapshot() {
  const [s, setS] = useState<CostSummary | null>(null);

  const load = useCallback(async () => {
    try {
      setS(await apiGet<CostSummary>("/cost-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!s || !s.has_data) return null;

  const peak = s.losses.reduce((m, l) => Math.max(m, l.cost), 0) || 1;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Cost of losses · last 7 days</h3>
          <p className="text-slate-400 text-sm mt-1">What downtime and scrap cost the plant this week</p>
        </div>
        <div className="text-right">
          <p className="text-3xl font-bold text-red-400">{money(s.loss_cost)}</p>
          <p className="text-[11px] text-slate-500">lost to downtime + scrap</p>
        </div>
      </div>

      <div className="mt-5 space-y-3">
        {s.losses.map((l) => {
          const isBiggest = l.key === s.biggest;
          const w = Math.max(2, Math.round((l.cost / peak) * 100));
          return (
            <div key={l.key}>
              <div className="flex items-center justify-between text-sm mb-1">
                <span className={isBiggest ? "text-amber-300 font-semibold" : "text-slate-300"}>
                  {l.label}{isBiggest ? " · biggest" : ""}
                </span>
                <span className="text-slate-400 tabular-nums">{money(l.cost)}</span>
              </div>
              <div className="h-2 rounded-full bg-slate-800 overflow-hidden">
                <div className={`h-full ${isBiggest ? "bg-amber-500" : "bg-red-500/70"}`} style={{ width: `${w}%` }} />
              </div>
              <p className="text-[11px] text-slate-500 mt-1">{l.detail}</p>
            </div>
          );
        })}
      </div>

      {s.by_type.length > 0 && (
        <div className="mt-4 pt-4 border-t border-slate-800/70">
          <p className="text-xs text-slate-500 mb-2">Recorded costs · {money(s.recorded_total)}</p>
          <div className="flex flex-wrap gap-2">
            {s.by_type.map((t) => (
              <span key={t.type} className="rounded-md border border-slate-700 bg-slate-800 px-2.5 py-1 text-xs text-slate-300">
                {t.type} <span className="text-slate-500">· {money(t.amount)}</span>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
