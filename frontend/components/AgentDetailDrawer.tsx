"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";

// Mirrors the backend agent detail read-model (ai/roster.py build_agent_detail).
type RecentAction = {
  id: number;
  action_type: string;
  summary: string;
  ref_kind: string;
  ref_id: number | null;
  severity: string;
  status: string;
  related_machine_id: number | null;
  created_at: string | null;
  decided_by: string | null;
  decided_at: string | null;
};

type AgentDetail = {
  key: string;
  name: string;
  watches: string;
  acts: string;
  auto_approves: boolean;
  total_actions: number;
  pending: number;
  approved: number;
  rejected: number;
  approval_rate: number | null;
  outputs: Record<string, number>;
  last_action_at: string | null;
  daily: { date: string; count: number }[];
  recent: RecentAction[];
};

function agentStyle(a: string) {
  if (a === "maintenance") return "bg-indigo-500/15 text-indigo-300 border-indigo-500/40";
  if (a === "reorder") return "bg-emerald-500/15 text-emerald-300 border-emerald-500/40";
  if (a === "quality") return "bg-orange-500/15 text-orange-300 border-orange-500/40";
  if (a === "escalation") return "bg-red-500/15 text-red-300 border-red-500/40";
  if (a === "yield") return "bg-purple-500/15 text-purple-300 border-purple-500/40";
  return "bg-slate-500/15 text-slate-300 border-slate-600/40";
}

function statusStyle(s: string) {
  if (s === "Proposed") return "border-amber-500/40 bg-amber-500/10 text-amber-300";
  if (s === "Approved") return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
  if (s === "Rejected") return "border-red-500/40 bg-red-500/10 text-red-300";
  return "border-slate-600/40 bg-slate-500/10 text-slate-300";
}

function fmt(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

function wk(iso: string) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { weekday: "short" });
}

function humanKind(k: string) {
  return k.replace(/_/g, " ");
}

export default function AgentDetailDrawer({
  agentKey,
  onClose,
  onChanged,
}: {
  agentKey: string;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDetail(await apiGet<AgentDetail>(`/agent-roster/${agentKey}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load agent detail");
    } finally {
      setLoading(false);
    }
  }, [agentKey]);

  useEffect(() => {
    load();
  }, [load]);

  // Close on Escape for keyboard users.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const decide = useCallback(
    async (id: number, decision: string) => {
      try {
        await apiPost(`/agent-actions/${id}/${decision}`, {});
        await load(); // refresh the drawer
        onChanged(); // and the section behind it
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to update the action");
      }
    },
    [load, onChanged],
  );

  const peak = detail ? detail.daily.reduce((m, d) => Math.max(m, d.count), 0) : 0;
  const outputEntries = detail ? Object.entries(detail.outputs) : [];

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} aria-hidden="true" />
      <div
        role="dialog"
        aria-modal="true"
        className="relative w-full max-w-xl bg-slate-950 border-l border-slate-800 h-full overflow-y-auto p-6"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 flex-wrap">
              <span className={`rounded-full px-3 py-1 text-sm border ${agentStyle(detail?.key ?? agentKey)}`}>
                {detail?.name ?? "Agent"}
              </span>
              {detail && (
                <span
                  className={`rounded-full px-2.5 py-1 text-[11px] border ${
                    detail.auto_approves
                      ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                      : "border-slate-600/40 bg-slate-500/10 text-slate-300"
                  }`}
                >
                  {detail.auto_approves ? "autonomous" : "needs approval"}
                </span>
              )}
            </div>
            <p className="text-slate-500 text-sm mt-2">AI agent cockpit — role, autonomy, and activity</p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl px-2" aria-label="Close">
            ✕
          </button>
        </div>

        {error && (
          <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-3 text-sm">{error}</div>
        )}

        {loading && !detail ? (
          <p className="text-slate-400 mt-6">Loading agent detail…</p>
        ) : detail ? (
          <div className="mt-5 space-y-6">
            {/* Role */}
            <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 space-y-1.5">
              <p className="text-sm text-slate-300">
                <span className="text-slate-500">Watches</span> — {detail.watches}
              </p>
              <p className="text-sm text-slate-300">
                <span className="text-slate-500">Acts</span> — {detail.acts}
              </p>
            </div>

            {/* Decision tally */}
            <div className="grid grid-cols-3 md:grid-cols-5 gap-3">
              <Stat label="Actions" value={detail.total_actions} />
              <Stat label="Pending" value={detail.pending} highlight={detail.pending > 0} />
              <Stat label="Approved" value={detail.approved} />
              <Stat label="Rejected" value={detail.rejected} />
              <Stat label="Approval" value={detail.approval_rate === null ? "—" : `${detail.approval_rate}%`} />
            </div>

            {/* What it produced */}
            {outputEntries.length > 0 && (
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Produced</h3>
                <div className="mt-2 flex flex-wrap gap-2">
                  {outputEntries.map(([kind, n]) => (
                    <span key={kind} className="rounded-lg border border-slate-700 px-2.5 py-1 text-xs text-slate-300">
                      {humanKind(kind)} <span className="text-slate-500">· {n}</span>
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* 7-day activity */}
            <div>
              <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Activity · last 7 days</h3>
              {peak === 0 ? (
                <p className="text-slate-500 text-sm mt-2">No actions in the last 7 days.</p>
              ) : (
                <div className="mt-3 flex items-end gap-2 h-20">
                  {detail.daily.map((d) => {
                    const h = peak ? Math.max(4, Math.round((d.count / peak) * 72)) : 4;
                    return (
                      <div
                        key={d.date}
                        className="flex-1 flex flex-col items-center justify-end gap-1"
                        title={`${d.count} on ${d.date}`}
                      >
                        <span className="text-[10px] text-slate-400">{d.count || ""}</span>
                        <div className="w-full bg-indigo-500/70 rounded-t" style={{ height: `${h}px` }} />
                        <span className="text-[10px] text-slate-500">{wk(d.date)}</span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Recent actions — approve/reject inline */}
            <div>
              <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Recent actions</h3>
              {detail.recent.length === 0 ? (
                <p className="text-slate-500 text-sm mt-2">This agent hasn’t acted yet.</p>
              ) : (
                <div className="mt-3 space-y-3">
                  {detail.recent.map((a) => (
                    <div key={a.id} className="rounded-xl border border-slate-800 bg-slate-900 p-4">
                      <div className="flex items-start justify-between gap-2">
                        <span className="text-sm text-slate-500">{a.action_type}</span>
                        <span className={`rounded-full px-2.5 py-0.5 text-[11px] border ${statusStyle(a.status)}`}>
                          {a.status}
                        </span>
                      </div>
                      <p className="text-sm font-medium mt-1.5">{a.summary}</p>
                      <div className="mt-2 flex items-center gap-2 text-xs text-slate-600 flex-wrap">
                        <span>{fmt(a.created_at)}</span>
                        {a.related_machine_id != null && <span>· machine #{a.related_machine_id}</span>}
                        {a.decided_by && <span>· by {a.decided_by}</span>}
                      </div>
                      {a.status === "Proposed" && (
                        <div className="mt-3 flex gap-2">
                          <button
                            onClick={() => decide(a.id, "approve")}
                            className="rounded-lg bg-emerald-500/90 text-slate-950 font-semibold px-3 py-1.5 text-sm hover:bg-emerald-400"
                          >
                            Approve
                          </button>
                          <button
                            onClick={() => decide(a.id, "reject")}
                            className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
                          >
                            Reject
                          </button>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function Stat({ label, value, highlight }: { label: string; value: string | number; highlight?: boolean }) {
  return (
    <div className={`rounded-xl border p-3 ${highlight ? "border-amber-500/40 bg-amber-500/10" : "border-slate-800 bg-slate-900/60"}`}>
      <p className="text-slate-400 text-[11px]">{label}</p>
      <p className={`text-xl font-bold mt-0.5 ${highlight ? "text-amber-300" : ""}`}>{value}</p>
    </div>
  );
}
