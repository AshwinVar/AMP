"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend scorecard read-model (ai/scorecard.py build_scorecard).
type Kpi = { key: string; label: string; value: number | null; unit: string; tone: "good" | "warn" | "bad" | "none" };
type Scorecard = { has_data: boolean; kpis: Kpi[] };

const toneCls: Record<string, string> = {
  good: "text-emerald-400",
  warn: "text-amber-400",
  bad: "text-red-400",
  none: "text-slate-300",
};

const fmt = (k: Kpi) =>
  k.value == null ? "—"
    : k.unit === "$" ? `$${k.value.toLocaleString()}`
    : k.unit === "%" ? `${k.value}%`
    : `${k.value}${k.unit}`;

// The executive scorecard strip: one headline KPI per pillar, colour-toned, at
// the top of the exec home. Self-contained — fetches its own summary and
// refreshes. Renders nothing until there's data.
export default function ScorecardStrip() {
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
      {s.kpis.map((k) => (
        <div key={k.key} className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
          <p className="text-[11px] uppercase tracking-wide text-slate-500">{k.label}</p>
          <p className={`text-3xl font-bold mt-1 ${toneCls[k.tone]}`}>{fmt(k)}</p>
        </div>
      ))}
    </div>
  );
}
