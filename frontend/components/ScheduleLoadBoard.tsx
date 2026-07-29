"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { parseApiDate } from "../lib/apiDate";

type ChaseRow = {
  schedule_no: string;
  machine: string;
  shift: string | null;
  priority: string;
  status: string;
  scheduled_date: string | null;
  planned_quantity: number;
  estimated_minutes: number;
  days_overdue: number;
  delayed: boolean;
};

// Mirrors ai.schedule_load.build_schedule_load (GET /schedule-load).
type ScheduleLoad = {
  horizon_days: number;
  scheduled_jobs: number;
  scheduled_minutes: number;
  by_day: { date: string; jobs: number; minutes: number }[];
  by_machine: { machine_id: number | null; name: string; line: string; jobs: number; minutes: number }[];
  by_shift: { shift: string; jobs: number; minutes: number }[];
  busiest_machine: { name: string; jobs: number; minutes: number } | null;
  peak_day: { date: string; jobs: number; minutes: number } | null;
  overdue: number;
  delayed: number;
  needs_attention: number;
  chase: ChaseRow[];
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
  const d = parseApiDate(iso);
  return d ? d.toLocaleDateString(undefined, { weekday: "narrow" }) : "";
}

// The planner's morning board: booked load over the next 7 days (by day and by
// machine), the peak, and what has already slipped. Self-contained — fetches
// /schedule-load itself and hides on error, so the CRUD section below always
// renders.
export default function ScheduleLoadBoard() {
  const [d, setD] = useState<ScheduleLoad | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setD(await apiGet<ScheduleLoad>("/schedule-load"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load the schedule board");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !d) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5 text-slate-400 text-sm">
        Loading the schedule load board…
      </div>
    );
  }
  if (error || !d) return null;

  const peakMinutes = d.by_day.reduce((m, x) => Math.max(m, x.minutes), 0);

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <h3 className="text-lg font-semibold">Load board — next {d.horizon_days} days</h3>
        <div className={`rounded-xl border px-4 py-2 text-sm ${toneClasses(d.tone)}`}>{d.verdict}</div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Jobs booked" value={d.scheduled_jobs} />
        <Stat label="Minutes booked" value={d.scheduled_minutes.toLocaleString()} />
        <Stat label="Past due (open)" value={d.overdue} bad={d.overdue > 0} />
        <Stat label="Marked delayed" value={d.delayed} bad={d.delayed > 0} />
      </div>

      {/* 8-day booked-minutes strip (today .. +7) */}
      <div className="flex items-end gap-1 h-24">
        {d.by_day.map((day, i) => {
          const h = peakMinutes ? Math.max(3, Math.round((day.minutes / peakMinutes) * 84)) : 3;
          const isPeak = d.peak_day?.date === day.date && day.minutes > 0;
          return (
            <div
              key={day.date}
              className="flex-1 flex flex-col items-center justify-end gap-1"
              title={`${day.jobs} job${day.jobs === 1 ? "" : "s"} · ${day.minutes.toLocaleString()} min on ${day.date}`}
            >
              <span className="text-[10px] text-slate-400">{day.minutes > 0 ? day.jobs : ""}</span>
              <div
                className={`w-full rounded-t ${isPeak ? "bg-amber-500/70" : "bg-sky-500/50"}`}
                style={{ height: `${h}px` }}
              />
              <span className={`text-[10px] ${i === 0 ? "text-white font-semibold" : "text-slate-500"}`}>
                {dayLabel(day.date)}
              </span>
            </div>
          );
        })}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Where the load lands */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Booked load by machine</h4>
          {d.by_machine.length === 0 ? (
            <p className="text-slate-500 text-sm">No jobs booked in the window.</p>
          ) : (
            <ul className="space-y-2">
              {d.by_machine.slice(0, 6).map((m) => {
                const top = d.by_machine[0]?.minutes || 1;
                const w = Math.max(4, Math.round((m.minutes / top) * 100));
                return (
                  <li key={`${m.machine_id}`} className="text-sm">
                    <div className="flex items-center justify-between gap-3">
                      <span className="min-w-0 truncate">
                        <span className="font-semibold">{m.name}</span>
                        {m.line && <span className="text-slate-500"> · {m.line}</span>}
                      </span>
                      <span className="whitespace-nowrap text-xs text-slate-400">
                        {m.jobs} job{m.jobs === 1 ? "" : "s"} · {m.minutes.toLocaleString()} min
                      </span>
                    </div>
                    <div className="mt-1 h-1.5 rounded bg-slate-800">
                      <div className="h-1.5 rounded bg-sky-500/60" style={{ width: `${w}%` }} />
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {/* What slipped */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">
            Slipped / delayed{d.needs_attention > d.chase.length ? ` (top ${d.chase.length} of ${d.needs_attention})` : ""}
          </h4>
          {d.chase.length === 0 ? (
            <p className="text-slate-500 text-sm">Nothing slipped — the board is on time.</p>
          ) : (
            <ul className="space-y-2">
              {d.chase.slice(0, 6).map((c) => (
                <li key={c.schedule_no} className="flex items-center justify-between gap-3 text-sm">
                  <span className="min-w-0 truncate">
                    <span className="font-semibold">{c.schedule_no}</span>
                    <span className="text-slate-400"> · {c.machine}{c.shift ? ` · ${c.shift}` : ""}</span>
                  </span>
                  <span className="whitespace-nowrap text-xs text-red-400">
                    {c.days_overdue > 0 ? `${c.days_overdue}d late` : "delayed"} · {c.priority}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}

function Stat({ label, value, bad }: { label: string; value: number | string; bad?: boolean }) {
  return (
    <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
      <p className="text-slate-400 text-xs">{label}</p>
      <h4 className={`text-2xl font-bold mt-1 ${bad ? "text-red-400" : ""}`}>{value}</h4>
    </div>
  );
}
