"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend read-model (ai/maintenance.py build_maintenance_execution).
type ByMachine = {
  machine_id: number; name: string;
  completed: number; on_time: number; compliance_rate: number | null; overdue: number;
};
type Chase = {
  task_no: string; machine: string; task_type: string; priority: string;
  status: string; planned_date: string | null; days_overdue: number; reactive: boolean;
};
type MaintenanceExecution = {
  days: number;
  completed: number;
  timed: number;
  on_time: number;
  late: number;
  compliance_rate: number | null;
  target: number;
  avg_days_late: number;
  worst_days_late: number;
  undated_completions: number;
  planned_count: number;
  reactive_count: number;
  planned_share: number | null;
  downtime_minutes: number;
  backlog: {
    open: number; overdue: number; oldest_days: number | null;
    aging: { bucket: string; count: number }[];
  };
  by_machine: ByMachine[];
  chase: Chase[];
  verdict: string;
  tone: "good" | "warn" | "bad";
};

const toneText: Record<string, string> = {
  good: "text-emerald-400", warn: "text-amber-400", bad: "text-red-400",
};
const toneBorder: Record<string, string> = {
  good: "border-l-emerald-500/70", warn: "border-l-amber-500/70", bad: "border-l-red-500/70",
};
const agingText: Record<string, string> = {
  "1-7 days": "text-yellow-400", "8-30 days": "text-orange-400", "30+ days": "text-red-400",
};

function complianceColor(pct: number | null, target: number) {
  if (pct == null) return "text-slate-400";
  if (pct >= target) return "text-emerald-400";
  if (pct >= 70) return "text-amber-400";
  return "text-red-400";
}

const hours = (m: number) => (m >= 60 ? `${(m / 60).toFixed(1)}h` : `${m}m`);

// Are we keeping the maintenance promise? The reliability card measures what the
// machines do to us; this one measures what we do about it — PM compliance over
// the window, how far the late work slipped, how much of it was firefighting
// rather than planned, and how stale the overdue backlog has gone. Self-contained:
// fetches its own read-model and refreshes. Renders nothing until there is
// maintenance history to judge.
export default function MaintenanceExecutionSnapshot({ onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [d, setD] = useState<MaintenanceExecution | null>(null);

  const load = useCallback(async () => {
    try {
      setD(await apiGet<MaintenanceExecution>("/maintenance-execution"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!d || (d.completed === 0 && d.backlog.open === 0)) return null;

  const plannedShare = d.planned_share ?? 0;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">
            Maintenance execution · {d.days} days
          </h3>
          <p className="text-slate-400 text-sm mt-1">
            {d.completed} completed
            {d.late > 0 ? ` · ${d.late} late` : ""}
            {d.backlog.overdue > 0 ? ` · ${d.backlog.overdue} overdue` : ""}
          </p>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${complianceColor(d.compliance_rate, d.target)}`}>
            {d.compliance_rate == null ? "—" : `${d.compliance_rate}%`}
          </p>
          <p className="text-[11px] text-slate-500">on plan · target {d.target}%</p>
        </div>
      </div>

      <div className={`mt-4 rounded-lg border border-slate-800 border-l-2 ${toneBorder[d.tone]} bg-slate-900/40 px-3 py-2`}>
        <p className={`text-sm ${toneText[d.tone]}`}>{d.verdict}</p>
      </div>

      {/* headline KPIs */}
      <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-2">
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className="text-xl font-bold text-slate-100">{d.on_time}<span className="text-sm text-slate-500">/{d.timed}</span></p>
          <p className="text-[11px] text-slate-500">on plan</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className={`text-xl font-bold ${d.avg_days_late > 0 ? "text-amber-400" : "text-slate-100"}`}>
            {d.avg_days_late > 0 ? `${d.avg_days_late}d` : "—"}
          </p>
          <p className="text-[11px] text-slate-500">avg slip</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className={`text-xl font-bold ${d.backlog.overdue > 0 ? "text-red-400" : "text-slate-100"}`}>{d.backlog.overdue}</p>
          <p className="text-[11px] text-slate-500">
            overdue{d.backlog.oldest_days ? ` · ${d.backlog.oldest_days}d oldest` : ""}
          </p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className="text-xl font-bold text-sky-300">{d.downtime_minutes > 0 ? hours(d.downtime_minutes) : "—"}</p>
          <p className="text-[11px] text-slate-500">maint. downtime</p>
        </div>
      </div>

      {/* planned vs reactive — the firefighting ratio */}
      {d.completed > 0 && (
        <div className="mt-5">
          <div className="flex items-center justify-between text-xs">
            <span className="text-slate-500">Planned vs reactive work</span>
            <span className="tabular-nums text-slate-400">
              {d.planned_count} planned · {d.reactive_count} reactive
            </span>
          </div>
          <div className="mt-1.5 flex h-2 overflow-hidden rounded-full bg-slate-800">
            <div className="bg-emerald-500/70" style={{ width: `${plannedShare}%` }} title={`${plannedShare}% planned`} />
            <div className="bg-orange-500/70" style={{ width: `${100 - plannedShare}%` }} title={`${100 - plannedShare}% reactive`} />
          </div>
        </div>
      )}

      <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* worst compliance by machine */}
        <div>
          <p className="text-xs text-slate-500 mb-2">Compliance by machine</p>
          {d.by_machine.length === 0 ? (
            <p className="text-slate-500 text-sm">No maintenance recorded against a machine.</p>
          ) : (
            <div className="space-y-2">
              {d.by_machine.map((m) => (
                <div key={m.machine_id} className="flex items-center justify-between rounded-lg border border-slate-800 px-3 py-2 text-sm">
                  <div className="min-w-0 flex-1">
                    <p className="text-slate-200 font-medium truncate">{m.name}</p>
                    <p className="text-[11px] text-slate-500">
                      {m.on_time}/{m.completed} on plan
                      {m.overdue > 0 ? ` · ${m.overdue} overdue` : ""}
                    </p>
                  </div>
                  <span className={`tabular-nums shrink-0 ${complianceColor(m.compliance_rate, d.target)}`}>
                    {m.compliance_rate == null ? "—" : `${m.compliance_rate}%`}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* the overdue backlog, oldest first */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <p className="text-xs text-slate-500">Overdue backlog</p>
            {onOpen && d.backlog.overdue > 0 && (
              <button
                type="button"
                onClick={() => onOpen("cmms")}
                className="rounded-md border border-slate-700 px-2 py-0.5 text-[11px] text-slate-300 hover:border-slate-500 hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
              >
                Open CMMS →
              </button>
            )}
          </div>
          {d.backlog.overdue === 0 ? (
            <p className="text-emerald-400 text-sm">Nothing overdue — the backlog is clean.</p>
          ) : (
            <>
              <div className="flex flex-wrap gap-2 mb-2">
                {d.backlog.aging.map((a) => (
                  <span key={a.bucket} className="rounded-md border border-slate-700 bg-slate-800 px-2.5 py-1 text-xs text-slate-300">
                    {a.bucket} <span className={agingText[a.bucket] ?? "text-slate-400"}>· {a.count}</span>
                  </span>
                ))}
              </div>
              <div className="space-y-2">
                {d.chase.map((t) => (
                  <div key={t.task_no} className="flex items-start gap-3 rounded-lg border border-slate-800 border-l-2 border-l-red-500/70 bg-slate-900/40 px-3 py-2">
                    <div className="min-w-0 flex-1">
                      <p className="text-sm text-slate-200 truncate">
                        {t.task_type} · <span className="text-slate-400">{t.machine}</span>
                      </p>
                      <p className="text-[11px] text-slate-500 truncate">
                        {t.task_no}{t.planned_date ? ` · planned ${t.planned_date}` : ""}
                      </p>
                    </div>
                    <span className="shrink-0 tabular-nums text-xs text-red-300">{t.days_overdue}d late</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      {d.undated_completions > 0 && (
        <p className="mt-4 text-[11px] text-slate-500">
          {d.undated_completions} completed task{d.undated_completions !== 1 ? "s" : ""} carry no completion date and
          can&apos;t be scored against plan.
        </p>
      )}
    </div>
  );
}
