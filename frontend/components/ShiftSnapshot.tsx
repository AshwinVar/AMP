"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend shift summary (ai/shift.py build_shift_summary).
type Shift = { shift: string; target: number; actual: number; entries: number; attainment: number };
type ShiftSummary = {
  days: number;
  entries: number;
  attainment: number;
  target: number;
  actual: number;
  shifts: Shift[];
  best: Shift | null;
  worst: Shift | null;
};

function attColor(v: number) {
  if (v >= 95) return "text-emerald-400";
  if (v >= 85) return "text-yellow-400";
  if (v >= 70) return "text-orange-400";
  return "text-red-400";
}

function barColor(v: number) {
  if (v >= 95) return "bg-emerald-500";
  if (v >= 85) return "bg-yellow-500";
  if (v >= 70) return "bg-orange-500";
  return "bg-red-500";
}

// A glanceable shift-attainment read-out — actual vs target per shift over the
// week. Self-contained: fetches its own summary and refreshes. Renders nothing
// until a shift has been logged.
export default function ShiftSnapshot() {
  const [s, setS] = useState<ShiftSummary | null>(null);

  const load = useCallback(async () => {
    try {
      setS(await apiGet<ShiftSummary>("/shift-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!s || s.entries === 0) return null;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Shift performance · last 7 days</h3>
          <p className="text-slate-400 text-sm mt-1">
            {s.actual.toLocaleString()} / {s.target.toLocaleString()} units
            {s.best && s.worst && s.best.shift !== s.worst.shift && (
              <> · best {s.best.shift} · watch {s.worst.shift}</>
            )}
          </p>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${attColor(s.attainment)}`}>{s.attainment}%</p>
          <p className="text-[11px] text-slate-500">attainment</p>
        </div>
      </div>

      <div className="mt-5 space-y-2">
        {s.shifts.map((sh) => (
          <div key={sh.shift} className="flex items-center gap-3">
            <span className="w-24 shrink-0 text-sm text-slate-300 truncate" title={sh.shift}>{sh.shift}</span>
            <div className="flex-1 h-2 rounded-full bg-slate-800 overflow-hidden">
              <div className={`h-full ${barColor(sh.attainment)}`} style={{ width: `${Math.min(100, sh.attainment)}%` }} />
            </div>
            <span className={`w-10 text-right text-sm font-semibold ${attColor(sh.attainment)}`}>{sh.attainment}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}
