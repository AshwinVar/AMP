"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

type Row = {
  task_no: string;
  machine: string;
  task_type: string;
  priority: string;
  planned_date: string;
  days_until?: number;
  days_overdue?: number;
};

// Mirrors ai.maintenance.build_maintenance_forecast (GET /maintenance-forecast).
type Forecast = {
  days: number;
  scheduled: number;
  overdue: number;
  due_today: number;
  due_next_7: number;
  peak: { date: string; count: number } | null;
  series: { date: string; count: number; urgent: number; is_today: boolean }[];
  by_machine: { machine_id: number; name: string; count: number; overdue: number }[];
  upcoming: Row[];
  overdue_tasks: Row[];
  verdict: string;
  tone: string;
};

function toneClasses(tone?: string) {
  switch (tone) {
    case "good": return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
    case "warn": return "border-amber-500/40 bg-amber-500/10 text-amber-300";
    case "bad": return "border-red-500/40 bg-red-500/10 text-red-300";
    default: return "border-slate-800 bg-slate-900 text-slate-300";
  }
}

function dayLabel(iso: string) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { weekday: "narrow" });
}

// The forward maintenance schedule for the CMMS view: the next 14 days laid out
// day by day, the overdue backlog to clear first, and the tasks to do next.
// Self-contained — fetches its own read-model; the CRUD table below is unchanged.
export default function MaintenanceForecastCard() {
  const [f, setF] = useState<Forecast | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setF(await apiGet<Forecast>("/maintenance-forecast"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load the maintenance forecast");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !f) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5 text-slate-400 text-sm">
        Loading the maintenance forecast…
      </div>
    );
  }
  if (error || !f) {
    return null; // the CRUD table below still renders; the card just stays hidden
  }

  const peakCount = f.series.reduce((m, d) => Math.max(m, d.count), 0);

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <h3 className="text-lg font-semibold">Forecast — next {f.days} days</h3>
        <div className={`rounded-xl border px-4 py-2 text-sm ${toneClasses(f.tone)}`}>{f.verdict}</div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Scheduled" value={f.scheduled} />
        <Stat label="Due today" value={f.due_today} />
        <Stat label="Next 7 days" value={f.due_next_7} />
        <Stat label="Overdue" value={f.overdue} tone={f.overdue > 0 ? "bad" : undefined} />
      </div>

      {/* 14-day load strip — bar height by count, the urgent (Critical/High) slice in red */}
      <div className="flex items-end gap-1 h-20">
        {f.series.map((d) => {
          const h = peakCount ? Math.max(3, Math.round((d.count / peakCount) * 72)) : 3;
          const urgentH = d.count ? Math.round((d.urgent / d.count) * h) : 0;
          return (
            <div
              key={d.date}
              className="flex-1 flex flex-col items-center justify-end gap-1"
              title={`${d.count} task${d.count === 1 ? "" : "s"} on ${d.date}${d.urgent ? ` (${d.urgent} urgent)` : ""}`}
            >
              <div className="w-full rounded-t bg-indigo-500/50 flex flex-col justify-end" style={{ height: `${h}px` }}>
                {urgentH > 0 && <div className="w-full rounded-t bg-red-500/70" style={{ height: `${urgentH}px` }} />}
              </div>
              <span className={`text-[10px] ${d.is_today ? "text-white font-semibold" : "text-slate-500"}`}>{dayLabel(d.date)}</span>
            </div>
          );
        })}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <TaskList
          title="Up next"
          empty="Nothing scheduled in the window."
          rows={f.upcoming}
          when={(r) => (r.days_until === 0 ? "today" : `in ${r.days_until}d`)}
        />
        <TaskList
          title="Overdue backlog"
          empty="No overdue tasks — on top of it."
          rows={f.overdue_tasks}
          when={(r) => `${r.days_overdue}d late`}
          danger
        />
      </div>
    </section>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
      <p className="text-slate-400 text-xs">{label}</p>
      <h4 className={`text-2xl font-bold mt-1 ${tone === "bad" && value > 0 ? "text-red-400" : ""}`}>{value}</h4>
    </div>
  );
}

function TaskList({ title, rows, empty, when, danger }: {
  title: string; rows: Row[]; empty: string; when: (r: Row) => string; danger?: boolean;
}) {
  return (
    <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
      <h4 className="text-sm font-semibold text-slate-300 mb-3">{title}</h4>
      {rows.length === 0 ? (
        <p className="text-slate-500 text-sm">{empty}</p>
      ) : (
        <ul className="space-y-2">
          {rows.map((r) => (
            <li key={r.task_no} className="flex items-center justify-between gap-3 text-sm">
              <span className="min-w-0">
                <span className="font-semibold">{r.task_no}</span>
                <span className="text-slate-400"> · {r.machine} · {r.task_type}</span>
              </span>
              <span className={`whitespace-nowrap text-xs ${danger ? "text-red-400" : "text-slate-400"}`}>
                {r.priority} · {when(r)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
