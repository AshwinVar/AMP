"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend scorecard read-model (ai/scorecard.py build_scorecard).
type Kpi = {
  key: string; label: string; value: number | null; unit: string;
  tone: "good" | "warn" | "bad" | "none";
  delta: number | null; delta_tone: "good" | "bad" | "flat" | null;
};
type Scorecard = { has_data: boolean; kpis: Kpi[] };

const toneCls: Record<string, string> = {
  good: "text-emerald-400",
  warn: "text-amber-400",
  bad: "text-red-400",
  none: "text-slate-300",
};

const deltaCls: Record<string, string> = {
  good: "text-emerald-400",
  bad: "text-red-400",
  flat: "text-slate-500",
};

const fmt = (k: Kpi) =>
  k.value == null ? "—"
    : k.unit === "$" ? `$${k.value.toLocaleString()}`
    : k.unit === "%" ? `${k.value}%`
    : `${k.value}${k.unit}`;

// The delta magnitude, formatted like the KPI (absolute value; the arrow carries the sign).
const fmtDelta = (k: Kpi) => {
  const a = Math.abs(k.delta as number);
  return k.unit === "$" ? `$${a.toLocaleString()}` : `${a}${k.unit === "%" ? "" : k.unit}`;
};
const deltaGlyph = (d: number) => (d > 0 ? "↑" : d < 0 ? "↓" : "→");

// Each KPI drills into the view that owns its detail.
const KPI_TO_VIEW: Record<string, string> = {
  oee: "executive",
  good_rate: "quality",
  on_time: "orders",
  loss_cost: "costing",
};

// The executive scorecard strip: one headline KPI per pillar, colour-toned, at
// the top of the exec home. Self-contained — fetches its own summary and
// refreshes. Renders nothing until there's data.
export default function ScorecardStrip({ onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [s, setS] = useState<Scorecard | null>(null);

  const load = useCallback(async () => {
    try {
      setS(await apiGet<Scorecard>("/scorecard"));
    } catch {
      // A glanceable strip — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!s || !s.has_data) return null;

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      {s.kpis.map((k) => {
        const view = KPI_TO_VIEW[k.key];
        const clickable = Boolean(onOpen && view);
        const cls = "group rounded-2xl border border-slate-800 bg-slate-900/60 p-5";
        const inner = (
          <>
            <p className="text-[11px] uppercase tracking-wide text-slate-500 flex items-center justify-between">
              {k.label}
              {clickable && <span className="text-slate-600 group-hover:text-slate-300 transition" aria-hidden>→</span>}
            </p>
            <p className={`text-3xl font-bold mt-1 ${toneCls[k.tone]}`}>{fmt(k)}</p>
            {k.delta != null && (
              <p className={`text-xs mt-0.5 ${deltaCls[k.delta_tone ?? "flat"]}`}>
                {deltaGlyph(k.delta)} {fmtDelta(k)} <span className="text-slate-600">vs last wk</span>
              </p>
            )}
          </>
        );
        return clickable ? (
          <button
            key={k.key}
            type="button"
            onClick={() => onOpen!(view)}
            title={`Open ${k.label}`}
            className={`${cls} w-full text-left hover:border-slate-600 hover:bg-slate-800/60 transition focus:outline-none focus:ring-2 focus:ring-slate-600`}
          >
            {inner}
          </button>
        ) : (
          <div key={k.key} className={cls}>{inner}</div>
        );
      })}
    </div>
  );
}
