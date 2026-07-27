"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors ai.downtime.build_downtime_summary (GET /downtime-summary).
type DowntimeSummary = {
  days: number;
  total_events: number;
  total_minutes: number;
  top_reasons: { reason: string; count: number; minutes: number }[];
  by_machine: { machine_id: number; name: string; count: number; minutes: number }[];
  by_line: { line: string; count: number; minutes: number }[];
};

// The shared week-over-week verdict trio (GET /downtime-trend).
type DowntimeTrend = { direction?: string; verdict?: string; tone?: string };

function toneClasses(tone?: string) {
  switch (tone) {
    case "good": return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
    case "warn": return "border-amber-500/40 bg-amber-500/10 text-amber-300";
    case "bad": return "border-red-500/40 bg-red-500/10 text-red-300";
    default: return "border-slate-800 bg-slate-900 text-slate-300";
  }
}

// The Downtime view's intelligence layer over the raw log: the 7-day picture
// (events, minutes lost), the reason Pareto, the machines losing the minutes,
// and the week-over-week verdict. Self-contained; hides on error so the log
// form below always renders.
export default function DowntimeIntelCard() {
  const [s, setS] = useState<DowntimeSummary | null>(null);
  const [t, setT] = useState<DowntimeTrend | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const [rs, rt] = await Promise.allSettled([
      apiGet<DowntimeSummary>("/downtime-summary"),
      apiGet<DowntimeTrend>("/downtime-trend"),
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
        Loading downtime intelligence…
      </div>
    );
  }
  if (!s) return null;

  const topReason = s.top_reasons[0]?.minutes || 1;

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <h3 className="text-lg font-semibold">Downtime — last {s.days} days</h3>
        {t?.verdict && (
          <div className={`rounded-xl border px-4 py-2 text-sm ${toneClasses(t.tone)}`}>{t.verdict}</div>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Events" value={s.total_events} />
        <Stat label="Minutes lost" value={s.total_minutes.toLocaleString()} bad={s.total_minutes > 0} />
        <Stat label="Top reason" value={s.top_reasons[0]?.reason || "—"} small />
        <Stat label="Worst machine" value={s.by_machine[0]?.name || "—"} small />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Reason Pareto by minutes */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Reason Pareto (minutes)</h4>
          {s.top_reasons.length === 0 ? (
            <p className="text-slate-500 text-sm">No downtime logged in the window.</p>
          ) : (
            <ul className="space-y-2">
              {s.top_reasons.slice(0, 6).map((r) => (
                <li key={r.reason} className="text-sm">
                  <div className="flex items-center justify-between gap-3">
                    <span className="min-w-0 truncate font-semibold">{r.reason}</span>
                    <span className="text-xs text-slate-400 whitespace-nowrap">
                      {r.minutes.toLocaleString()} min · {r.count} event{r.count === 1 ? "" : "s"}
                    </span>
                  </div>
                  <div className="mt-1 h-1.5 rounded bg-slate-800">
                    <div
                      className="h-1.5 rounded bg-red-500/60"
                      style={{ width: `${Math.max(4, Math.round((r.minutes / topReason) * 100))}%` }}
                    />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Machines losing the minutes */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Machines losing the minutes</h4>
          {s.by_machine.length === 0 ? (
            <p className="text-slate-500 text-sm">No downtime attributed to machines.</p>
          ) : (
            <ul className="space-y-2">
              {s.by_machine.slice(0, 6).map((m) => (
                <li key={m.machine_id} className="flex items-center justify-between gap-3 text-sm">
                  <span className="min-w-0 truncate font-semibold">{m.name}</span>
                  <span className="whitespace-nowrap text-xs text-slate-400">
                    {m.minutes.toLocaleString()} min · {m.count} event{m.count === 1 ? "" : "s"}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {s.by_line.length > 0 && (
            <p className="text-xs text-slate-500 mt-3">
              By line: {s.by_line.map((l) => `${l.line}: ${l.minutes.toLocaleString()} min`).join(" · ")}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

function Stat({ label, value, bad, small }: { label: string; value: number | string; bad?: boolean; small?: boolean }) {
  return (
    <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
      <p className="text-slate-400 text-xs">{label}</p>
      <h4 className={`${small ? "text-base" : "text-2xl"} font-bold mt-1 truncate ${bad ? "text-red-400" : ""}`}>{value}</h4>
    </div>
  );
}
