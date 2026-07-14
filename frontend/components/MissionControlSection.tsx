"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPatch, apiPost } from "../lib/api";

// Mirrors the backend Insight shape (ai/insights.py build_feed).
type Insight = {
  source: string;            // "recommendation" | "event" | "action"
  kind: string;              // recommendation_type or event_type
  severity: string;          // Critical | High | Medium | Low | Info
  title: string;
  message: string;
  occurred_at: string;       // ISO-8601
  related_machine_id: number | null;
  ref_id: number | null;     // recommendation id (for actioning); null for events
};

function severityStyle(sev: string) {
  if (sev === "Critical") return "border-red-500/40 bg-red-500/10 text-red-300";
  if (sev === "High") return "border-orange-500/40 bg-orange-500/10 text-orange-300";
  if (sev === "Medium") return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
  if (sev === "Low") return "border-green-500/40 bg-green-500/10 text-green-300";
  return "border-slate-600/40 bg-slate-500/10 text-slate-300"; // Info
}

function sourceBadge(source: string) {
  if (source === "recommendation")
    return { label: "AI recommendation", cls: "bg-indigo-500/15 text-indigo-300 border-indigo-500/40" };
  if (source === "action")
    return { label: "Agent action", cls: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40" };
  return { label: "Event", cls: "bg-slate-500/15 text-slate-300 border-slate-600/40" };
}

function timeAgo(iso: string) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export default function MissionControlSection() {
  const [insights, setInsights] = useState<Insight[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setInsights(await apiGet<Insight[]>("/insights"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load insights");
    } finally {
      setLoading(false);
    }
  }, []);

  const act = useCallback(async (refId: number, status: string) => {
    try {
      await apiPatch(`/ai/recommendations/${refId}`, { status });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to update the recommendation");
    }
  }, [load]);

  const decideAction = useCallback(async (id: number, decision: string) => {
    try {
      await apiPost(`/agent-actions/${id}/${decision}`, {});
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to update the agent action");
    }
  }, [load]);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000); // keep the command feed fresh
    return () => clearInterval(id);
  }, [load]);

  const counts = {
    total: insights.length,
    critical: insights.filter((i) => i.severity === "Critical").length,
    high: insights.filter((i) => i.severity === "High").length,
    recommendations: insights.filter((i) => i.source === "recommendation").length,
  };

  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Mission Control</h2>
          <p className="text-slate-400 mt-2">
            What the factory needs to know now — AI recommendations, notable events, and agent actions, unified and live.
          </p>
        </div>
        <button onClick={load} className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">
          Refresh
        </button>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Kpi title="Active insights" value={counts.total} />
        <Kpi title="Critical" value={counts.critical} />
        <Kpi title="High" value={counts.high} />
        <Kpi title="AI recommendations" value={counts.recommendations} />
      </div>

      {error && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-4">{error}</div>
      )}

      {loading && insights.length === 0 ? (
        <p className="text-slate-400">Loading the feed…</p>
      ) : insights.length === 0 ? (
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-10 text-center">
          <div className="text-3xl mb-3">✓</div>
          <h3 className="text-lg font-semibold">All clear</h3>
          <p className="text-slate-400 mt-1 text-sm">No open recommendations or notable events right now.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {insights.map((i, idx) => {
            const badge = sourceBadge(i.source);
            return (
              <div key={idx} className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={`rounded-full px-3 py-1 text-xs border ${badge.cls}`}>{badge.label}</span>
                    <span className="text-sm text-slate-500">{i.kind}</span>
                  </div>
                  <span className={`rounded-full px-3 py-1 text-xs border ${severityStyle(i.severity)}`}>{i.severity}</span>
                </div>
                <h3 className="text-xl font-bold mt-3">{i.title}</h3>
                <p className="text-slate-300 mt-2">{i.message}</p>
                <div className="mt-3 flex items-center gap-3 text-sm text-slate-500">
                  <span>{timeAgo(i.occurred_at)}</span>
                  {i.related_machine_id != null && <span>· machine #{i.related_machine_id}</span>}
                </div>
                {i.source === "recommendation" && i.ref_id != null && (
                  <div className="mt-4 flex gap-2">
                    <button onClick={() => act(i.ref_id as number, "Acknowledged")}
                      className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800">
                      Acknowledge
                    </button>
                    <button onClick={() => act(i.ref_id as number, "Closed")}
                      className="rounded-lg bg-white text-slate-950 font-semibold px-3 py-1.5 text-sm">
                      Resolve
                    </button>
                  </div>
                )}
                {i.source === "action" && i.ref_id != null && (
                  <div className="mt-4 flex gap-2">
                    <button onClick={() => decideAction(i.ref_id as number, "approve")}
                      className="rounded-lg bg-emerald-500/90 text-slate-950 font-semibold px-3 py-1.5 text-sm hover:bg-emerald-400">
                      Approve
                    </button>
                    <button onClick={() => decideAction(i.ref_id as number, "reject")}
                      className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800">
                      Reject
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
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
