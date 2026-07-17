"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

type Outputs = { maintenance_tasks: number; purchase_orders: number; escalations: number };

type AgentContribution = {
  agent: string;
  name: string;
  actions: number;
  approved: number;
  auto_approved: number;
  pending: number;
  outputs: Outputs;
};

// Mirrors the backend impact rollup (ai/impact.py build_impact).
type Impact = {
  agents_active: string[];
  total_actions: number;
  approved: number;
  rejected: number;
  auto_approved: number;
  auto_rate: number;
  pending_backlog: number;
  outputs: Outputs;
  by_agent: AgentContribution[];
  last_7_days: { total: number; proposed: number; approved: number; rejected: number };
  headline: string;
};

type Trend = { days: number; total: number; peak: number; daily: { date: string; count: number }[] };

function agentStyle(a: string) {
  if (a === "maintenance") return "bg-indigo-500/15 text-indigo-300 border-indigo-500/40";
  if (a === "reorder") return "bg-emerald-500/15 text-emerald-300 border-emerald-500/40";
  if (a === "quality") return "bg-orange-500/15 text-orange-300 border-orange-500/40";
  if (a === "escalation") return "bg-red-500/15 text-red-300 border-red-500/40";
  if (a === "yield") return "bg-purple-500/15 text-purple-300 border-purple-500/40";
  return "bg-slate-500/15 text-slate-300 border-slate-600/40";
}

function weekday(iso: string) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { weekday: "short" });
}

// The exec/sales artifact for the AI workforce: what the agents have actually
// produced, how much ran without a human, who's contributed what, and the
// week's activity. Self-contained — fetches the impact rollup + activity trend.
export default function AgentRoiSection() {
  const [imp, setImp] = useState<Impact | null>(null);
  const [trend, setTrend] = useState<Trend | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [i, t] = await Promise.all([
        apiGet<Impact>("/agent-actions/impact"),
        apiGet<Trend>("/agent-actions/trend"),
      ]);
      setImp(i);
      setTrend(t);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load agent impact");
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  return (
    <section className="mt-8 space-y-6">
      <div>
        <h2 className="text-3xl font-bold">AI Workforce ROI</h2>
        <p className="text-slate-400 mt-2">What your autonomous agents have done for you — outputs, autonomy, and who's contributing.</p>
      </div>

      {error && <div className="rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-4">{error}</div>}

      {imp && (
        <>
          <div className="rounded-2xl border border-indigo-500/30 bg-gradient-to-br from-indigo-500/10 to-slate-900 p-6">
            <p className="text-xs font-semibold uppercase tracking-wide text-indigo-300">Your AI workforce</p>
            <p className="text-lg font-semibold mt-1">{imp.headline}</p>
            <div className="mt-5 grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4">
              <Tile label="Actions taken" value={imp.total_actions} />
              <Tile label="Ran autonomously" value={`${imp.auto_rate}%`} sub="of decisions" accent />
              <Tile label="Tasks opened" value={imp.outputs.maintenance_tasks} />
              <Tile label="POs drafted" value={imp.outputs.purchase_orders} />
              <Tile label="Escalations" value={imp.outputs.escalations} />
              <Tile label="Awaiting you" value={imp.pending_backlog} highlight={imp.pending_backlog > 0} />
            </div>
          </div>

          {trend && trend.total > 0 && (
            <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-5">
              <div className="flex items-center justify-between">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Activity · last 7 days</h3>
                <span className="text-xs text-slate-500">{trend.total} action{trend.total !== 1 ? "s" : ""}</span>
              </div>
              <div className="mt-4 flex items-end gap-2 h-24">
                {trend.daily.map((d) => {
                  const h = trend.peak ? Math.max(4, Math.round((d.count / trend.peak) * 96)) : 4;
                  return (
                    <div key={d.date} className="flex-1 flex flex-col items-center justify-end gap-1" title={`${d.count} on ${d.date}`}>
                      <span className="text-[10px] text-slate-400">{d.count || ""}</span>
                      <div className="w-full bg-indigo-500/70 rounded-t" style={{ height: `${h}px` }} />
                      <span className="text-[10px] text-slate-500">{weekday(d.date)}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Contribution by agent</h3>
            {imp.by_agent.length === 0 ? (
              <div className="mt-3 rounded-2xl border border-slate-800 bg-slate-900/60 p-10 text-center">
                <div className="text-3xl mb-3">🤖</div>
                <p className="text-slate-400 text-sm">No agent activity yet — contributions appear here as the agents act.</p>
              </div>
            ) : (
              <div className="mt-3 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                {imp.by_agent.map((a) => (
                  <div key={a.agent} className="rounded-2xl border border-slate-800 bg-slate-900 p-5">
                    <div className="flex items-center justify-between gap-2">
                      <span className={`rounded-full px-3 py-1 text-xs border ${agentStyle(a.agent)}`}>{a.name}</span>
                      <span className="text-2xl font-bold">{a.actions}</span>
                    </div>
                    <p className="text-[11px] text-slate-500 mt-1 text-right">actions</p>
                    <div className="mt-3 flex flex-wrap gap-2 text-xs">
                      {a.outputs.maintenance_tasks > 0 && (
                        <span className="rounded-lg border border-slate-700 px-2.5 py-1 text-slate-300">{a.outputs.maintenance_tasks} task{a.outputs.maintenance_tasks !== 1 ? "s" : ""}</span>
                      )}
                      {a.outputs.purchase_orders > 0 && (
                        <span className="rounded-lg border border-slate-700 px-2.5 py-1 text-slate-300">{a.outputs.purchase_orders} PO{a.outputs.purchase_orders !== 1 ? "s" : ""}</span>
                      )}
                      {a.outputs.escalations > 0 && (
                        <span className="rounded-lg border border-slate-700 px-2.5 py-1 text-slate-300">{a.outputs.escalations} escalation{a.outputs.escalations !== 1 ? "s" : ""}</span>
                      )}
                    </div>
                    <div className="mt-3 flex items-center gap-4 text-xs text-slate-400 flex-wrap">
                      {a.auto_approved > 0 && <span className="text-emerald-300">{a.auto_approved} auto</span>}
                      <span>{a.approved} approved</span>
                      {a.pending > 0 && <span className="text-amber-300">{a.pending} pending</span>}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}

function Tile({ label, value, sub, accent, highlight }: { label: string; value: string | number; sub?: string; accent?: boolean; highlight?: boolean }) {
  return (
    <div className={`rounded-xl border p-4 ${highlight ? "border-amber-500/40 bg-amber-500/10" : accent ? "border-emerald-500/40 bg-emerald-500/10" : "border-slate-800 bg-slate-900/60"}`}>
      <p className="text-slate-400 text-xs">{label}</p>
      <p className={`text-2xl font-bold mt-1 ${highlight ? "text-amber-300" : accent ? "text-emerald-300" : ""}`}>{value}</p>
      {sub && <p className="text-[11px] text-slate-500 mt-0.5">{sub}</p>}
    </div>
  );
}
