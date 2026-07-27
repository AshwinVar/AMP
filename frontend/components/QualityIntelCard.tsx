"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors ai.quality.build_quality_summary (GET /quality-summary).
type QualitySummary = {
  inspections: number;
  inspected: number;
  passed: number;
  failed: number;
  rework: number;
  scrap: number;
  first_pass_yield: number | null;
  fail_rate: number | null;
  top_defects: { category: string; count: number }[];
  by_machine: { machine_id: number; name: string; inspected: number; failed: number; fail_rate: number | null }[];
  by_line: { line: string; inspected: number; failed: number; fail_rate: number | null }[];
};

// The shared week-over-week verdict trio (GET /quality-trend).
type QualityTrend = { direction?: string; verdict?: string; tone?: string };

function toneClasses(tone?: string) {
  switch (tone) {
    case "good": return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
    case "warn": return "border-amber-500/40 bg-amber-500/10 text-amber-300";
    case "bad": return "border-red-500/40 bg-red-500/10 text-red-300";
    default: return "border-slate-800 bg-slate-900 text-slate-300";
  }
}

// The Quality view's intelligence layer over the inspection CRUD: the 7-day
// picture (FPY, fail rate, rework/scrap), the defect Pareto, the machines
// producing the failures, and the week-over-week verdict. Self-contained;
// hides on error so the CRUD below always renders.
export default function QualityIntelCard() {
  const [s, setS] = useState<QualitySummary | null>(null);
  const [t, setT] = useState<QualityTrend | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const [rs, rt] = await Promise.allSettled([
      apiGet<QualitySummary>("/quality-summary"),
      apiGet<QualityTrend>("/quality-trend"),
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
        Loading quality intelligence…
      </div>
    );
  }
  if (!s) return null;

  const topDefect = s.top_defects[0]?.count || 1;

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <h3 className="text-lg font-semibold">Quality — last 7 days</h3>
        {t?.verdict && (
          <div className={`rounded-xl border px-4 py-2 text-sm ${toneClasses(t.tone)}`}>{t.verdict}</div>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat
          label="First-pass yield"
          value={s.first_pass_yield === null ? "—" : `${s.first_pass_yield}%`}
        />
        <Stat
          label="Fail rate"
          value={s.fail_rate === null ? "—" : `${s.fail_rate}%`}
          bad={s.fail_rate !== null && s.fail_rate > 0}
        />
        <Stat label="Rework units" value={s.rework.toLocaleString()} warn={s.rework > 0} />
        <Stat label="Scrap units" value={s.scrap.toLocaleString()} bad={s.scrap > 0} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Defect Pareto */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Defect Pareto</h4>
          {s.top_defects.length === 0 ? (
            <p className="text-slate-500 text-sm">No failed units in the window.</p>
          ) : (
            <ul className="space-y-2">
              {s.top_defects.slice(0, 6).map((d) => (
                <li key={d.category} className="text-sm">
                  <div className="flex items-center justify-between gap-3">
                    <span className="min-w-0 truncate font-semibold">{d.category}</span>
                    <span className="text-xs text-slate-400 whitespace-nowrap">{d.count.toLocaleString()} units</span>
                  </div>
                  <div className="mt-1 h-1.5 rounded bg-slate-800">
                    <div
                      className="h-1.5 rounded bg-orange-500/60"
                      style={{ width: `${Math.max(4, Math.round((d.count / topDefect) * 100))}%` }}
                    />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Where the failures come from */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Machines producing the failures</h4>
          {s.by_machine.length === 0 ? (
            <p className="text-slate-500 text-sm">No inspections recorded in the window.</p>
          ) : (
            <ul className="space-y-2">
              {s.by_machine.slice(0, 6).map((m) => (
                <li key={m.machine_id} className="flex items-center justify-between gap-3 text-sm">
                  <span className="min-w-0 truncate font-semibold">{m.name}</span>
                  <span className="whitespace-nowrap text-xs text-slate-400">
                    {m.failed.toLocaleString()}/{m.inspected.toLocaleString()} failed
                    {m.fail_rate !== null && (
                      <span className={m.fail_rate > 0 ? " text-red-400" : ""}> · {m.fail_rate}%</span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {s.by_line.length > 0 && (
            <p className="text-xs text-slate-500 mt-3">
              By line: {s.by_line.map((l) => `${l.line}: ${l.fail_rate === null ? "—" : `${l.fail_rate}%`}`).join(" · ")}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

function Stat({ label, value, bad, warn }: { label: string; value: number | string; bad?: boolean; warn?: boolean }) {
  return (
    <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
      <p className="text-slate-400 text-xs">{label}</p>
      <h4 className={`text-2xl font-bold mt-1 ${bad ? "text-red-400" : warn ? "text-amber-400" : ""}`}>{value}</h4>
    </div>
  );
}
