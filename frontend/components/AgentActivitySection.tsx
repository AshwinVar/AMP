"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";

// Mirrors the backend AgentAction (main.py _agent_action_dict).
type AgentAction = {
  id: number;
  agent: string;
  action_type: string;
  summary: string;
  ref_kind: string;
  ref_id: number | null;
  severity: string;
  status: string;
  related_machine_id: number | null;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
};

const FILTERS = ["All", "Proposed", "Approved", "Rejected"];

function statusStyle(s: string) {
  if (s === "Proposed") return "border-amber-500/40 bg-amber-500/10 text-amber-300";
  if (s === "Approved") return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
  if (s === "Rejected") return "border-red-500/40 bg-red-500/10 text-red-300";
  return "border-slate-600/40 bg-slate-500/10 text-slate-300";
}

function agentStyle(a: string) {
  if (a === "maintenance") return "bg-indigo-500/15 text-indigo-300 border-indigo-500/40";
  if (a === "reorder") return "bg-emerald-500/15 text-emerald-300 border-emerald-500/40";
  if (a === "quality") return "bg-orange-500/15 text-orange-300 border-orange-500/40";
  return "bg-slate-500/15 text-slate-300 border-slate-600/40";
}

function fmt(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

export default function AgentActivitySection() {
  const [rows, setRows] = useState<AgentAction[]>([]);
  const [filter, setFilter] = useState("All");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const q = filter === "All" ? "" : `?status=${filter}`;
      setRows(await apiGet<AgentAction[]>(`/agent-actions${q}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load agent activity");
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    load();
  }, [load]);

  const decide = useCallback(async (id: number, decision: string) => {
    try {
      await apiPost(`/agent-actions/${id}/${decision}`, {});
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to update the action");
    }
  }, [load]);

  return (
    <section className="mt-8 space-y-6">
      <div>
        <h2 className="text-3xl font-bold">Agent Activity</h2>
        <p className="text-slate-400 mt-2">
          Every autonomous agent action — proposed, approved or rejected — with a full audit trail.
        </p>
      </div>

      <div className="flex gap-2 flex-wrap">
        {FILTERS.map((f) => (
          <button key={f} onClick={() => setFilter(f)}
            className={`rounded-lg px-3 py-1.5 text-sm border ${
              filter === f
                ? "bg-white text-slate-950 border-white font-semibold"
                : "border-slate-700 text-slate-300 hover:bg-slate-800"
            }`}>
            {f}
          </button>
        ))}
      </div>

      {error && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-4">{error}</div>
      )}

      {loading && rows.length === 0 ? (
        <p className="text-slate-400">Loading activity…</p>
      ) : rows.length === 0 ? (
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-10 text-center">
          <div className="text-3xl mb-3">🤖</div>
          <h3 className="text-lg font-semibold">No agent activity{filter !== "All" ? ` (${filter})` : ""}</h3>
          <p className="text-slate-400 mt-1 text-sm">Agents log every action here as they act.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {rows.map((a) => (
            <div key={a.id} className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className={`rounded-full px-3 py-1 text-xs border ${agentStyle(a.agent)}`}>{a.agent} agent</span>
                  <span className="text-sm text-slate-500">{a.action_type}</span>
                </div>
                <span className={`rounded-full px-3 py-1 text-xs border ${statusStyle(a.status)}`}>{a.status}</span>
              </div>
              <h3 className="text-lg font-semibold mt-3">{a.summary}</h3>
              <div className="mt-3 flex items-center gap-3 text-sm text-slate-500 flex-wrap">
                <span>proposed {fmt(a.created_at)}</span>
                {a.decided_by && <span>· {a.status.toLowerCase()} by {a.decided_by} {fmt(a.decided_at)}</span>}
                {a.related_machine_id != null && <span>· machine #{a.related_machine_id}</span>}
              </div>
              {a.status === "Proposed" && (
                <div className="mt-4 flex gap-2">
                  <button onClick={() => decide(a.id, "approve")}
                    className="rounded-lg bg-emerald-500/90 text-slate-950 font-semibold px-3 py-1.5 text-sm hover:bg-emerald-400">
                    Approve
                  </button>
                  <button onClick={() => decide(a.id, "reject")}
                    className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800">
                    Reject
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
