"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import MachineDetailDrawer from "./MachineDetailDrawer";

// Mirrors the backend twin (ai/twin.py build_twins).
type Twin = {
  machine_id: number;
  name: string;
  status: string;
  utilization: number;
  downtime: string;
  health_score: number;
  health_band: string;
  risk_score: number;
  risk_level: string;
  top_reason: string;
  open_maintenance_tasks: number;
  pending_agent_actions: number;
  recent_downtime: { reason: string; duration: string }[];
};

function bandStyle(band: string) {
  if (band === "Healthy") return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
  if (band === "Watch") return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
  if (band === "At risk") return "border-orange-500/40 bg-orange-500/10 text-orange-300";
  return "border-red-500/40 bg-red-500/10 text-red-300"; // Critical
}

function healthColor(score: number) {
  if (score >= 80) return "text-emerald-400";
  if (score >= 55) return "text-yellow-400";
  if (score >= 35) return "text-orange-400";
  return "text-red-400";
}

function statusDot(status: string) {
  if (status === "Running") return "bg-emerald-400";
  if (status === "Breakdown") return "bg-red-400";
  if (status === "Maintenance") return "bg-blue-400";
  if (status === "Idle") return "bg-yellow-400";
  return "bg-slate-400";
}

export default function MachineHealthSection() {
  const [twins, setTwins] = useState<Twin[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setTwins(await apiGet<Twin[]>("/machine-health"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load machine health");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000); // live twin, refreshed
    return () => clearInterval(id);
  }, [load]);

  const avg = twins.length ? Math.round(twins.reduce((s, t) => s + t.health_score, 0) / twins.length) : 0;
  const attention = twins.filter((t) => t.health_band === "Critical" || t.health_band === "At risk").length;
  const pending = twins.reduce((s, t) => s + t.pending_agent_actions, 0);

  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Machine Health</h2>
          <p className="text-slate-400 mt-2">
            A live twin per machine — state, health score, risk, and what the agents want to do about it.
          </p>
        </div>
        <button onClick={load} className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Refresh</button>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Kpi title="Machines" value={twins.length} />
        <Kpi title="Avg health" value={avg} />
        <Kpi title="Need attention" value={attention} />
        <Kpi title="Pending actions" value={pending} />
      </div>

      {error && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-4">{error}</div>
      )}

      {loading && twins.length === 0 ? (
        <p className="text-slate-400">Loading machine twins…</p>
      ) : twins.length === 0 ? (
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-10 text-center">
          <div className="text-3xl mb-3">▦</div>
          <h3 className="text-lg font-semibold">No machines yet</h3>
          <p className="text-slate-400 mt-1 text-sm">Machine twins appear here as soon as machines are registered.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {twins.map((t) => (
            <button
              key={t.machine_id}
              type="button"
              onClick={() => setSelected(t.machine_id)}
              className="text-left rounded-2xl bg-slate-900 border border-slate-800 p-5 hover:border-slate-600 hover:bg-slate-900/80 transition focus:outline-none focus:ring-2 focus:ring-slate-500"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-2">
                  <span className={`h-2.5 w-2.5 rounded-full ${statusDot(t.status)}`} />
                  <h3 className="text-xl font-bold">{t.name}</h3>
                </div>
                <span className={`rounded-full px-3 py-1 text-xs border ${bandStyle(t.health_band)}`}>{t.health_band}</span>
              </div>
              <div className="mt-4 flex items-end gap-4">
                <div>
                  <p className={`text-4xl font-bold ${healthColor(t.health_score)}`}>{t.health_score}</p>
                  <p className="text-xs text-slate-500">health score</p>
                </div>
                <div className="text-sm text-slate-400 space-y-0.5 pb-1">
                  <p>{t.status} · util {t.utilization}%</p>
                  <p>risk {t.risk_level} ({t.risk_score})</p>
                </div>
              </div>
              <p className="text-sm text-slate-400 mt-3">{t.top_reason}</p>
              <div className="mt-4 flex gap-2 flex-wrap text-xs">
                {t.open_maintenance_tasks > 0 && (
                  <span className="rounded-full border border-slate-700 px-3 py-1 text-slate-300">
                    {t.open_maintenance_tasks} maintenance task{t.open_maintenance_tasks > 1 ? "s" : ""}
                  </span>
                )}
                {t.pending_agent_actions > 0 && (
                  <span className="rounded-full border border-amber-500/40 bg-amber-500/10 text-amber-300 px-3 py-1">
                    {t.pending_agent_actions} pending approval{t.pending_agent_actions > 1 ? "s" : ""}
                  </span>
                )}
                {t.open_maintenance_tasks === 0 && t.pending_agent_actions === 0 && (
                  <span className="text-slate-500">no open work</span>
                )}
              </div>
            </button>
          ))}
        </div>
      )}
      {selected != null && (
        <MachineDetailDrawer
          machineId={selected}
          onClose={() => setSelected(null)}
          onChanged={load}
        />
      )}
    </section>
  );
}

function Kpi({ title, value }: { title: string; value: string | number }) {
  return (
    <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <p className="text-slate-400 text-sm">{title}</p>
      <h3 className="text-2xl font-bold mt-2">{value}</h3>
    </div>
  );
}
