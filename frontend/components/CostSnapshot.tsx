"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { barPct } from "../lib/bar";
import { lossFigure, money } from "../lib/money";

// Mirrors the backend cost read-model (ai/cost.py build_cost_summary).
//
// Losses are GOOD UNITS NOT MADE, and money only at the tenant's own unit value
// (ADR-0010). Every *_cost field is null when no unit value is set, and every
// units field is null when downtime had no run time to convert. The card used to
// print a fixed £12 a minute and £25 a unit for every tenant.
type Loss = { key: string; label: string; units: number | null; cost: number | null; detail: string };
type LossRow = {
  downtime_minutes: number;
  rejected_units: number;
  downtime_lost_units: number | null;
  lost_units: number | null;
  downtime_cost: number | null;
  scrap_cost: number | null;
  cost: number | null;
};
export type CostSummary = {
  has_data: boolean;
  priced: boolean;
  unit_value_gbp: number | null;
  loss_cost: number | null;
  lost_units: number | null;
  losses: Loss[];
  biggest: string | null;
  by_line: ({ line: string } & LossRow)[];
  by_machine: ({ machine_id: number; name: string } & LossRow)[];
  // `partial` marks the oldest bar when the rolling 7x24h window opens
  // mid-day, so the series covers the eight calendar dates it touches and
  // sums back to the headline (backend test_cost_bars_explain_headline.py).
  daily: { date: string; cost: number | null; lost_units: number | null; partial?: boolean }[];
  recorded_total: number;
  by_type: { type: string; amount: number }[];
};

// SMT and IC each get a consistent accent across the dashboard (sky / violet).
const lineChip = (line: string) =>
  line === "SMT"
    ? "border-sky-500/40 bg-sky-500/10 text-sky-300"
    : line === "IC"
    ? "border-violet-500/40 bg-violet-500/10 text-violet-300"
    : "border-slate-700 bg-slate-800 text-slate-300";

const split = (r: LossRow) =>
  `Downtime ${lossFigure(r.downtime_cost, r.downtime_lost_units)} · Scrap ${lossFigure(r.scrap_cost, r.rejected_units)}`;

// The cost-of-losses card: what downtime and scrap cost the plant this week, in
// good units and (with a unit value) money, plus the costs actually recorded.
// Self-contained — fetches its own summary and refreshes. Renders nothing until
// there's something to show.
export default function CostSnapshot({ onOpen }: { onOpen?: (viewKey: string) => void }) {
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

  // Bars scale by lost units: with one rate, the £ order is the unit order, and
  // units exist whether or not a rate is set.
  const peak = s.losses.reduce((m, l) => Math.max(m, l.units ?? 0), 0) || 1;
  const dailyPeak = Math.max(...s.daily.map((d) => d.lost_units ?? 0), 1);

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Cost of losses · last 7 days</h3>
          <p className="text-slate-400 text-sm mt-1">
            {s.priced
              ? "What downtime and scrap cost the plant this week"
              : "Good units downtime and scrap cost the plant this week — set a unit value to see money"}
          </p>
          {onOpen && (
            <button
              type="button"
              onClick={() => onOpen("costing")}
              className="mt-2 rounded-md border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:border-slate-500 hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
            >
              Manage costs →
            </button>
          )}
        </div>
        <div className="text-right">
          <p className="text-3xl font-bold text-red-400">{lossFigure(s.loss_cost, s.lost_units)}</p>
          <p className="text-[11px] text-slate-500">
            {s.lost_units == null ? "downtime with no run time to convert" : "lost to downtime + scrap"}
          </p>
        </div>
      </div>

      <div className="mt-5 space-y-3">
        {s.losses.map((l) => {
          const isBiggest = l.key === s.biggest;
          // A zero component (e.g. no scrap all week) renders an empty bar, not a
          // 2% sliver next to its zero — the display-honesty guard #464 applied to
          // the daily sparklines. A nonzero loss keeps its 2% floor.
          const w = barPct(l.units ?? 0, peak, 2);
          return (
            <div key={l.key}>
              <div className="flex items-center justify-between text-sm mb-1">
                <span className={isBiggest ? "text-amber-300 font-semibold" : "text-slate-300"}>
                  {l.label}{isBiggest ? " · biggest" : ""}
                </span>
                <span className="text-slate-400 tabular-nums">{lossFigure(l.cost, l.units)}</span>
              </div>
              <div className="h-2 rounded-full bg-slate-800 overflow-hidden">
                <div className={`h-full ${isBiggest ? "bg-amber-500" : "bg-red-500/70"}`} style={{ width: `${w}%` }} />
              </div>
              <p className="text-[11px] text-slate-500 mt-1">{l.detail}</p>
            </div>
          );
        })}
      </div>

      {s.daily.some((d) => (d.lost_units ?? 0) > 0) && (
        <div className="mt-4">
          <p className="text-xs text-slate-500 mb-1.5">Daily · last 7 days</p>
          <div className="flex items-end gap-1 h-12">
            {s.daily.map((d) => (
              <div
                key={d.date}
                className={`flex-1 rounded-sm bg-red-500/60 ${d.partial ? "opacity-50" : ""}`}
                // A zero-loss day (no downtime, no scrap) renders no bar, not a
                // 3% phantom sliver that reads as a loss that never happened —
                // the twin of the downtime daily series #464 already guarded.
                // A nonzero day keeps its 3% floor to stay visible.
                style={{ height: `${barPct(d.lost_units ?? 0, dailyPeak, 3)}%` }}
                title={`${d.date}: ${lossFigure(d.cost, d.lost_units)}${d.partial ? " (partial day — the 7-day window opens part-way through it)" : ""}`}
              />
            ))}
          </div>
        </div>
      )}

      {s.by_line.length > 1 && (
        <div className="mt-4 pt-4 border-t border-slate-800/70">
          <p className="text-xs text-slate-500 mb-2">By line</p>
          <div className="flex flex-wrap gap-2">
            {s.by_line.map((l) => (
              <span
                key={l.line}
                className={`rounded-md border px-2.5 py-1 text-xs font-medium ${lineChip(l.line)}`}
                title={split(l)}
              >
                {l.line} <span className="opacity-70">· {lossFigure(l.cost, l.lost_units)}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {s.by_machine.length > 0 && (
        <div className="mt-4 pt-4 border-t border-slate-800/70">
          <p className="text-xs text-slate-500 mb-2">Biggest losses by machine</p>
          <div className="flex flex-wrap gap-2">
            {s.by_machine.map((m) => {
              const cls = "rounded-md border border-slate-700 bg-slate-800 px-2.5 py-1 text-xs text-slate-300";
              const label = (
                <>
                  {m.name} <span className="text-slate-500">· {lossFigure(m.cost, m.lost_units)}</span>
                </>
              );
              return onOpen ? (
                <button
                  key={m.machine_id}
                  type="button"
                  onClick={() => onOpen("machines")}
                  title={`${split(m)} — open Machines`}
                  className={`${cls} hover:border-slate-500 hover:bg-slate-700 transition focus:outline-none focus:ring-2 focus:ring-slate-600`}
                >
                  {label}
                </button>
              ) : (
                <span key={m.machine_id} className={cls} title={split(m)}>
                  {label}
                </span>
              );
            })}
          </div>
        </div>
      )}

      {s.by_type.length > 0 && (
        <div className="mt-4 pt-4 border-t border-slate-800/70">
          {/* Recorded costs are the tenant's own logged amounts: money whatever the rate. */}
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
