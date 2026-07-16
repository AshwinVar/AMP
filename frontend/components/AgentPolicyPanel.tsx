"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPut, getUserRole } from "../lib/api";

// Mirrors the backend policy read-model (ai/roster.py build_agent_policy).
type PolicyAgent = { key: string; name: string; watches: string; acts: string; auto_approves: boolean };
type Policy = { source: "tenant" | "default"; agents: PolicyAgent[] };

function agentStyle(a: string) {
  if (a === "maintenance") return "bg-indigo-500/15 text-indigo-300 border-indigo-500/40";
  if (a === "reorder") return "bg-emerald-500/15 text-emerald-300 border-emerald-500/40";
  if (a === "quality") return "bg-orange-500/15 text-orange-300 border-orange-500/40";
  if (a === "escalation") return "bg-red-500/15 text-red-300 border-red-500/40";
  if (a === "yield") return "bg-purple-500/15 text-purple-300 border-purple-500/40";
  return "bg-slate-500/15 text-slate-300 border-slate-600/40";
}

// The human-in-control panel for the AI workforce: which agents may act on their
// own vs. wait for approval. Reads the tenant policy; an Admin can toggle each
// agent (the change is saved immediately and re-drives the whole platform's
// auto-approve behaviour). Everyone else sees it read-only. Self-contained.
export default function AgentPolicyPanel() {
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [saving, setSaving] = useState<string | null>(null); // key mid-toggle
  const [error, setError] = useState<string | null>(null);
  const canEdit = getUserRole() === "Admin";

  const load = useCallback(async () => {
    try {
      setPolicy(await apiGet<Policy>("/agent-policy"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load the autonomy policy");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const toggle = useCallback(
    async (key: string) => {
      if (!policy || !canEdit) return;
      const next = new Set(policy.agents.filter((a) => a.auto_approves).map((a) => a.key));
      if (next.has(key)) next.delete(key);
      else next.add(key);
      setSaving(key);
      setError(null);
      try {
        setPolicy(await apiPut<Policy>("/agent-policy", { auto_approve: [...next] }));
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to update the policy");
      } finally {
        setSaving(null);
      }
    },
    [policy, canEdit],
  );

  if (!policy) return null;

  const autonomous = policy.agents.filter((a) => a.auto_approves).length;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Agent autonomy</h3>
          <p className="text-slate-400 text-sm mt-1">
            Who acts on their own vs. waits for your approval · {autonomous} of {policy.agents.length} autonomous
            {policy.source === "default" && " · platform default"}
          </p>
        </div>
        {!canEdit && <span className="text-[11px] text-slate-500 mt-1">Admin only</span>}
      </div>

      {error && (
        <div className="mt-3 rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-3 text-sm">{error}</div>
      )}

      <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-3">
        {policy.agents.map((a) => (
          <div
            key={a.key}
            className="flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900 p-4"
          >
            <div className="min-w-0">
              <span className={`rounded-full px-2.5 py-0.5 text-xs border ${agentStyle(a.key)}`}>{a.name}</span>
              <p className="text-xs text-slate-500 mt-1.5 truncate" title={a.acts}>{a.acts}</p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={a.auto_approves}
              disabled={!canEdit || saving === a.key}
              onClick={() => toggle(a.key)}
              title={
                a.auto_approves
                  ? "Acts autonomously — click to require your approval"
                  : "Needs your approval — click to let it act on its own"
              }
              className={`shrink-0 relative inline-flex h-6 w-11 items-center rounded-full transition disabled:opacity-50 disabled:cursor-not-allowed ${
                a.auto_approves ? "bg-emerald-500" : "bg-slate-700"
              }`}
            >
              <span
                className={`inline-block h-4 w-4 transform rounded-full bg-white transition ${
                  a.auto_approves ? "translate-x-6" : "translate-x-1"
                }`}
              />
            </button>
          </div>
        ))}
      </div>

      <p className="text-[11px] text-slate-600 mt-3">
        Autonomous agents apply their low-risk actions immediately; the rest post to your inbox for approval.
      </p>
    </div>
  );
}
