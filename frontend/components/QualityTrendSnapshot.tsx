"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend read-model (ai/quality.py build_quality_trend).
type Half = { inspections: number; inspected: number; failed: number; fail_rate: number };
type Point = { date: string; inspected: number; failed: number; fail_rate: number };
type Mover = {
  machine_id: number; name: string;
  fail_rate: number; prior_fail_rate: number; delta_pts: number;
  inspected: number; failed: number;
};
type DefectMover = {
  category: string; failed: number; prior_failed: number; delta: number; is_new: boolean;
};
type QualityTrend = {
  days: number;
  half_days: number;
  current: Half;
  prior: Half;
  delta_pts: number | null;
  direction: "worsening" | "improving" | "steady" | "unknown";
  units_swing: number;
  thin_sample: boolean;
  drift_threshold_pts: number;
  series: Point[];
  drifting: Mover[];
  drifting_count: number;
  improving: Mover[];
  unscored_machines: number;
  defect_movers: DefectMover[];
  verdict: string;
  tone: "good" | "warn" | "bad";
};

const toneText: Record<string, string> = {
  good: "text-emerald-400", warn: "text-amber-400", bad: "text-red-400",
};
const toneBorder: Record<string, string> = {
  good: "border-l-emerald-500/70", warn: "border-l-amber-500/70", bad: "border-l-red-500/70",
};
// A rising fail rate is bad — the opposite of the usual "up is good" colouring.
const deltaText = (pts: number) =>
  pts > 0 ? "text-red-400" : pts < 0 ? "text-emerald-400" : "text-slate-100";
const signed = (n: number) => `${n > 0 ? "+" : ""}${n}`;
const arrow: Record<string, string> = {
  worsening: "↑", improving: "↓", steady: "→", unknown: "·",
};
const dayLabel = (iso: string) => iso.slice(5).replace("-", "/");

// Which way is quality going, and who moved it? The quality snapshot reports the
// level; this one reports the direction — this week's fail rate against last
// week's on the same basis, the extra units the swing costs at current volume,
// and the machines and defect categories behind it. Self-contained: fetches its
// own read-model and refreshes. Renders nothing until there is something to plot.
export default function QualityTrendSnapshot() {
  const [d, setD] = useState<QualityTrend | null>(null);

  const load = useCallback(async () => {
    try {
      setD(await apiGet<QualityTrend>("/quality-trend"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!d || d.current.inspected + d.prior.inspected === 0) return null;

  // The real worst-day fail rate, shown as the label (never floored — a floor is a
  // rendering guard, not a measurement, so it must not leak into the number).
  const truePeak = Math.max(...d.series.map((s) => s.fail_rate), 0);
  // Bars are scaled against the worst day; floor only the DIVISOR so a sub-1% week
  // still reads without inventing a peak.
  const scale = Math.max(truePeak, 0.1);

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">
            Quality trend · {d.days} days
          </h3>
          <p className="text-slate-400 text-sm mt-1">
            {d.current.inspected.toLocaleString()} units inspected this week
            {d.prior.inspected > 0 ? ` · ${d.prior.inspected.toLocaleString()} last week` : ""}
          </p>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${d.delta_pts == null ? "text-slate-400" : deltaText(d.delta_pts)}`}>
            {d.delta_pts == null ? "—" : `${arrow[d.direction]} ${signed(d.delta_pts)}`}
          </p>
          <p className="text-[11px] text-slate-500">pts fail rate · week on week</p>
        </div>
      </div>

      <div className={`mt-4 rounded-lg border border-slate-800 border-l-2 ${toneBorder[d.tone]} bg-slate-900/40 px-3 py-2`}>
        <p className={`text-sm ${toneText[d.tone]}`}>{d.verdict}</p>
      </div>

      {/* headline KPIs */}
      <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-2">
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className="text-xl font-bold text-slate-100">{d.current.fail_rate}%</p>
          <p className="text-[11px] text-slate-500">fail rate now</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className="text-xl font-bold text-slate-400">
            {d.prior.inspected > 0 ? `${d.prior.fail_rate}%` : "—"}
          </p>
          <p className="text-[11px] text-slate-500">prior {d.half_days} days</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className={`text-xl font-bold ${d.units_swing > 0 ? "text-red-400" : d.units_swing < 0 ? "text-emerald-400" : "text-slate-100"}`}>
            {d.units_swing === 0 ? "—" : signed(d.units_swing)}
          </p>
          <p className="text-[11px] text-slate-500">units failing vs last week</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className={`text-xl font-bold ${d.drifting_count > 0 ? "text-amber-400" : "text-slate-100"}`}>
            {d.drifting_count}
          </p>
          <p className="text-[11px] text-slate-500">machines drifting</p>
        </div>
      </div>

      {/* daily fail rate across the window — the two halves shaded apart */}
      <div className="mt-5">
        <div className="flex items-center justify-between text-xs">
          <span className="text-slate-500">Daily fail rate</span>
          <span className="text-slate-500">peak {truePeak}%</span>
        </div>
        <div className="mt-1.5 flex h-16 items-end gap-1">
          {d.series.map((s, idx) => (
            <div
              key={s.date}
              className="flex-1 rounded-t-sm bg-slate-800"
              style={{ height: "100%" }}
              title={`${s.date}: ${s.inspected} inspected, ${s.failed} failed (${s.fail_rate}%)`}
            >
              <div className="flex h-full flex-col justify-end">
                <div
                  className={idx < d.half_days ? "bg-slate-600" : "bg-sky-500/80"}
                  style={{ height: `${Math.round((s.fail_rate / scale) * 100)}%`, minHeight: s.inspected > 0 ? 2 : 0 }}
                />
              </div>
            </div>
          ))}
        </div>
        <div className="mt-1 flex justify-between text-[10px] text-slate-600">
          <span>{dayLabel(d.series[0].date)}</span>
          <span>{dayLabel(d.series[d.series.length - 1].date)}</span>
        </div>
      </div>

      <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* who moved — worst drift first, then the best improver for contrast */}
        <div>
          <p className="text-xs text-slate-500 mb-2">Machines that moved</p>
          {d.drifting.length === 0 && d.improving.length === 0 ? (
            <p className="text-slate-500 text-sm">
              No machine moved more than {d.drift_threshold_pts} pts on enough volume to judge.
            </p>
          ) : (
            <div className="space-y-2">
              {[...d.drifting, ...d.improving].map((m) => (
                <div
                  key={m.machine_id}
                  className={`flex items-center justify-between rounded-lg border border-slate-800 border-l-2 ${m.delta_pts > 0 ? "border-l-red-500/70" : "border-l-emerald-500/70"} px-3 py-2 text-sm`}
                >
                  <div className="min-w-0 flex-1">
                    <p className="text-slate-200 font-medium truncate">{m.name}</p>
                    <p className="text-[11px] text-slate-500">
                      {m.prior_fail_rate}% → {m.fail_rate}% · {m.inspected.toLocaleString()} inspected
                    </p>
                  </div>
                  <span className={`tabular-nums shrink-0 ${deltaText(m.delta_pts)}`}>
                    {signed(m.delta_pts)} pts
                  </span>
                </div>
              ))}
            </div>
          )}
          {d.unscored_machines > 0 && (
            <p className="mt-2 text-[11px] text-slate-500">
              {d.unscored_machines} machine{d.unscored_machines !== 1 ? "s" : ""} inspected too few units in
              one week to score.
            </p>
          )}
        </div>

        {/* what moved — defect categories by growth in failed units */}
        <div>
          <p className="text-xs text-slate-500 mb-2">Defects by growth</p>
          {d.defect_movers.length === 0 ? (
            <p className="text-emerald-400 text-sm">No failures recorded in the window.</p>
          ) : (
            <div className="space-y-2">
              {d.defect_movers.map((f) => (
                <div key={f.category} className="flex items-center justify-between rounded-lg border border-slate-800 px-3 py-2 text-sm">
                  <div className="min-w-0 flex-1">
                    <p className="text-slate-200 font-medium truncate">
                      {f.category}
                      {f.is_new && (
                        <span className="ml-2 rounded border border-amber-500/50 px-1.5 py-0.5 text-[10px] text-amber-400 align-middle">
                          new
                        </span>
                      )}
                    </p>
                    <p className="text-[11px] text-slate-500">
                      {f.prior_failed} → {f.failed} units failed
                    </p>
                  </div>
                  <span className={`tabular-nums shrink-0 ${deltaText(f.delta)}`}>{signed(f.delta)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
