"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost, apiPatch } from "../lib/api";

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
};

// Mirrors the backend NotificationResponse (schemas.py).
type Notif = {
  id: number;
  notification_type: string;
  severity: string;
  title: string;
  message: string;
  status: string;
  created_at: string | null;
};

function severityStyle(sev: string) {
  if (sev === "Critical") return "border-red-500/40 bg-red-500/10 text-red-300";
  if (sev === "High") return "border-orange-500/40 bg-orange-500/10 text-orange-300";
  if (sev === "Medium") return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
  if (sev === "Low") return "border-green-500/40 bg-green-500/10 text-green-300";
  return "border-slate-600/40 bg-slate-500/10 text-slate-300";
}

function agentStyle(a: string) {
  if (a === "maintenance") return "bg-indigo-500/15 text-indigo-300 border-indigo-500/40";
  if (a === "reorder") return "bg-emerald-500/15 text-emerald-300 border-emerald-500/40";
  if (a === "quality") return "bg-orange-500/15 text-orange-300 border-orange-500/40";
  if (a === "escalation") return "bg-red-500/15 text-red-300 border-red-500/40";
  if (a === "yield") return "bg-purple-500/15 text-purple-300 border-purple-500/40";
  return "bg-slate-500/15 text-slate-300 border-slate-600/40";
}

function fmt(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

// One place to triage everything awaiting a human: the agent approval queue
// (approve/reject inline) and the notifications feed (mark read). Self-contained
// — fetches its own data and refreshes.
export default function ApprovalsInbox() {
  const [approvals, setApprovals] = useState<AgentAction[]>([]);
  const [notifs, setNotifs] = useState<Notif[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [a, n] = await Promise.all([
        apiGet<AgentAction[]>("/agent-actions?status=Proposed"),
        apiGet<Notif[]>("/notifications"),
      ]);
      setApprovals(a);
      setNotifs(n);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load the inbox");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  const decide = useCallback(
    async (id: number, decision: string) => {
      try {
        await apiPost(`/agent-actions/${id}/${decision}`, {});
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to update the action");
      }
    },
    [load],
  );

  const markRead = useCallback(
    async (id: number) => {
      try {
        await apiPatch(`/notifications/${id}`, { status: "Read" });
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to update the notification");
      }
    },
    [load],
  );

  const unread = notifs.filter((n) => n.status !== "Read");

  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Inbox</h2>
          <p className="text-slate-400 mt-2">Everything awaiting you — agent approvals and notifications, in one place.</p>
        </div>
        <button onClick={load} className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3">Refresh</button>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <Kpi title="Pending approvals" value={approvals.length} highlight={approvals.length > 0} />
        <Kpi title="Unread notifications" value={unread.length} />
      </div>

      {error && <div className="rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-4">{error}</div>}

      {/* Awaiting your approval */}
      <div>
        <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Awaiting your approval</h3>
        {loading && approvals.length === 0 ? (
          <p className="text-slate-400 mt-3">Loading…</p>
        ) : approvals.length === 0 ? (
          <div className="mt-3 rounded-2xl border border-slate-800 bg-slate-900/60 p-8 text-center">
            <div className="text-2xl mb-2">✓</div>
            <p className="text-slate-400 text-sm">Nothing to approve — the agents are all caught up.</p>
          </div>
        ) : (
          <div className="mt-3 space-y-3">
            {approvals.map((a) => (
              <div key={a.id} className="rounded-2xl border border-amber-500/30 bg-amber-500/5 p-5">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={`rounded-full px-3 py-1 text-xs border ${agentStyle(a.agent)}`}>{a.agent} agent</span>
                    <span className="text-sm text-slate-500">{a.action_type}</span>
                  </div>
                  <span className={`rounded-full px-3 py-1 text-xs border ${severityStyle(a.severity)}`}>{a.severity}</span>
                </div>
                <h4 className="text-lg font-semibold mt-3">{a.summary}</h4>
                <div className="mt-2 text-sm text-slate-500">
                  proposed {fmt(a.created_at)}
                  {a.related_machine_id != null && <> · machine #{a.related_machine_id}</>}
                </div>
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
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Notifications */}
      <div>
        <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Notifications</h3>
        {notifs.length === 0 ? (
          <p className="text-slate-500 text-sm mt-3">No notifications.</p>
        ) : (
          <div className="mt-3 space-y-2">
            {notifs.map((n) => {
              const isUnread = n.status !== "Read";
              return (
                <div key={n.id} className={`rounded-xl border p-4 ${isUnread ? "border-slate-700 bg-slate-900" : "border-slate-800 bg-slate-900/40 opacity-70"}`}>
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={`rounded-full px-2.5 py-0.5 text-[11px] border ${severityStyle(n.severity)}`}>{n.severity}</span>
                      <span className="text-xs text-slate-500">{n.notification_type}</span>
                    </div>
                    {isUnread && (
                      <button onClick={() => markRead(n.id)} className="text-xs text-slate-400 hover:text-white">Mark read</button>
                    )}
                  </div>
                  <p className="font-medium mt-2">{n.title}</p>
                  <p className="text-sm text-slate-400 mt-0.5">{n.message}</p>
                  <p className="text-xs text-slate-600 mt-1">{fmt(n.created_at)}</p>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}

function Kpi({ title, value, highlight }: { title: string; value: number; highlight?: boolean }) {
  return (
    <div className={`rounded-2xl border p-5 ${highlight ? "border-amber-500/40 bg-amber-500/10" : "border-slate-800 bg-slate-900"}`}>
      <p className="text-slate-400 text-sm">{title}</p>
      <h3 className={`text-2xl font-bold mt-2 ${highlight ? "text-amber-300" : ""}`}>{value}</h3>
    </div>
  );
}
