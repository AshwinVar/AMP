"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend maintenance read-model (ai/maintenance.py build_maintenance_summary).
type Task = {
  task_no: string; machine: string; task_type: string; priority: string;
  status: string; planned_date: string | null; overdue: boolean; proposed: boolean;
};
type MaintenanceSummary = {
  open: number;
  pending_approval: number;
  overdue: number;
  by_priority: { priority: string; count: number }[];
  by_machine: { machine_id: number; name: string; count: number }[];
  tasks: Task[];
};

const prioText: Record<string, string> = {
  Critical: "text-red-400", High: "text-orange-400", Medium: "text-yellow-400", Low: "text-slate-400",
};
const prioChip: Record<string, string> = {
  Critical: "border-red-500/40 bg-red-500/10 text-red-300",
  High: "border-orange-500/40 bg-orange-500/10 text-orange-300",
  Medium: "border-yellow-500/40 bg-yellow-500/10 text-yellow-300",
  Low: "border-slate-700 bg-slate-800 text-slate-300",
};

// A glanceable maintenance read-out — open load, what's overdue, what the agent
// is waiting on approval for, and the tasks to do next. Self-contained: fetches
// its own summary and refreshes. Renders nothing when there's no open work.
export default function MaintenanceSnapshot({ onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [s, setS] = useState<MaintenanceSummary | null>(null);

  const load = useCallback(async () => {
    try {
      setS(await apiGet<MaintenanceSummary>("/maintenance-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!s || s.open === 0) return null;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Maintenance</h3>
          <p className="text-slate-400 text-sm mt-1">
            {s.open} open task{s.open !== 1 ? "s" : ""}
            {s.overdue > 0 ? ` · ${s.overdue} overdue` : ""}
          </p>
        </div>
        {onOpen && (
          <button
            type="button"
            onClick={() => onOpen("cmms")}
            className="rounded-md border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:border-slate-500 hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
          >
            Open CMMS →
          </button>
        )}
      </div>

      {/* headline counts */}
      <div className="mt-4 grid grid-cols-3 gap-2">
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2.5 text-center">
          <p className="text-xl font-bold text-slate-100">{s.open}</p>
          <p className="text-[11px] text-slate-500">open</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2.5 text-center">
          <p className={`text-xl font-bold ${s.overdue > 0 ? "text-red-400" : "text-slate-100"}`}>{s.overdue}</p>
          <p className="text-[11px] text-slate-500">overdue</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2.5 text-center">
          <p className={`text-xl font-bold ${s.pending_approval > 0 ? "text-amber-400" : "text-slate-100"}`}>{s.pending_approval}</p>
          <p className="text-[11px] text-slate-500">awaiting approval</p>
        </div>
      </div>

      {s.by_priority.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-2">
          {s.by_priority.map((p) => (
            <span key={p.priority} className={`rounded-md border px-2.5 py-1 text-xs font-medium ${prioChip[p.priority] ?? prioChip.Low}`}>
              {p.priority} <span className="opacity-70">· {p.count}</span>
            </span>
          ))}
        </div>
      )}

      {s.by_machine.length > 0 && (
        <div className="mt-4">
          <p className="text-xs text-slate-500 mb-2">By machine</p>
          <div className="flex flex-wrap gap-2">
            {s.by_machine.map((m) => {
              const cls = "rounded-md border border-slate-700 bg-slate-800 px-2.5 py-1 text-xs text-slate-300";
              const label = <>{m.name} <span className="text-slate-500">· {m.count}</span></>;
              return onOpen ? (
                <button
                  key={m.machine_id}
                  type="button"
                  onClick={() => onOpen("machines")}
                  title={`${m.count} open task${m.count !== 1 ? "s" : ""} — open Machines`}
                  className={`${cls} hover:border-slate-500 hover:bg-slate-700 transition focus:outline-none focus:ring-2 focus:ring-slate-600`}
                >
                  {label}
                </button>
              ) : (
                <span key={m.machine_id} className={cls}>{label}</span>
              );
            })}
          </div>
        </div>
      )}

      <div className="mt-4">
        <p className="text-xs text-slate-500 mb-2">Next up</p>
        <div className="space-y-2">
          {s.tasks.map((t) => (
            <div
              key={t.task_no}
              className={`flex items-start gap-3 rounded-lg border border-slate-800 border-l-2 ${t.overdue ? "border-l-red-500/70" : "border-l-slate-600"} bg-slate-900/40 px-3 py-2`}
            >
              <div className="min-w-0 flex-1">
                <p className="text-sm text-slate-200 truncate">
                  {t.task_type} · <span className="text-slate-400">{t.machine}</span>
                </p>
                <p className="text-[11px] text-slate-500 truncate">
                  {t.task_no}{t.planned_date ? ` · planned ${t.planned_date}` : ""}
                </p>
              </div>
              {t.proposed && (
                <span className="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-300">approve</span>
              )}
              {t.overdue && (
                <span className="shrink-0 rounded bg-red-500/15 px-1.5 py-0.5 text-[10px] font-medium text-red-300">overdue</span>
              )}
              <span className={`shrink-0 text-xs font-medium ${prioText[t.priority] ?? prioText.Low}`}>{t.priority}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
