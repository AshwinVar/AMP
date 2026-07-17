"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend losses read-model (ai/losses.py build_losses_summary).
type Loss = { key: string; label: string; points: number; detail: string };
type LossesSummary = { has_data: boolean; oee: number; total_loss: number; losses: Loss[]; biggest: string | null };

// The OEE losses card: where the gap from 100% is going, in real terms.
// Self-contained — fetches its own summary and refreshes. Renders nothing until
// there's production to measure.
export default function LossesSnapshot() {
  const [s, setS] = useState<LossesSummary | null>(null);

  const load = useCallback(async () => {
    try {
      setS(await apiGet<LossesSummary>("/losses-summary"));
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

  const peak = s.losses.reduce((m, l) => Math.max(m, l.points), 0) || 1;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">OEE losses · last 7 days</h3>
          <p className="text-slate-400 text-sm mt-1">
            OEE is {s.oee}% — here's where the missing {s.total_loss} points are going
          </p>
        </div>
        <div className="text-right">
          <p className="text-3xl font-bold text-orange-400">{s.total_loss}</p>
          <p className="text-[11px] text-slate-500">OEE pts lost</p>
        </div>
      </div>

      <div className="mt-5 space-y-3">
        {s.losses.map((l) => {
          const isBiggest = l.key === s.biggest;
          const w = Math.max(2, Math.round((l.points / peak) * 100));
          return (
            <div key={l.key}>
              <div className="flex items-center justify-between text-sm mb-1">
                <span className={isBiggest ? "text-amber-300 font-semibold" : "text-slate-300"}>
                  {l.label}
                  {isBiggest ? " · biggest loss" : ""}
                </span>
                <span className="text-slate-400 tabular-nums">{l.points} pts</span>
              </div>
              <div className="h-2 rounded-full bg-slate-800 overflow-hidden">
                <div className={`h-full ${isBiggest ? "bg-amber-500" : "bg-orange-500/70"}`} style={{ width: `${w}%` }} />
              </div>
              <p className="text-[11px] text-slate-500 mt-1">{l.detail}</p>
            </div>
          );
        })}
      </div>
    </div>
  );
}
