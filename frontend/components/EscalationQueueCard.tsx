"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

type QueueRow = {
  id: number;
  title: string;
  severity: string;
  department: string | null;
  owner: string | null;
  status: string;
  machine: string | null;
  days_open: number;
  agent_raised: boolean;
};

// Mirrors ai.escalations.build_escalation_summary (GET /escalation-summary).
type EscalationSummary = {
  days: number;
  stale_open_days: number;
  open: number;
  pending_approval: number;
  urgent_open: number;
  agent_raised: number;
  manual_raised: number;
  oldest_open_days: number | null;
  aging: { bucket: string; count: number }[];
  queue: QueueRow[];
  resolution: {
    raised: number;
    resolved: number;
    cancelled: number;
    resolution_rate: number | null;
    timed_resolutions: number;
    avg_resolution_hours: number | null;
    worst_resolution_hours: number | null;
  };
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

function sevClasses(s: string) {
  switch (s) {
    case "Critical": return "text-red-400";
    case "High": return "text-orange-400";
    case "Medium": return "text-amber-400";
    default: return "text-slate-400";
  }
}

// The ops lead's queue picture: how big the backlog is, how stale, and — the
// question the raw table can't answer — are we clearing escalations faster than
// they arrive (30-day resolution scorecard with measured, never inferred,
// time-to-resolve). Self-contained; hides on error so the table below always
// renders.
export default function EscalationQueueCard({ onOpen }: { onOpen?: (id: number) => void }) {
  const [d, setD] = useState<EscalationSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setD(await apiGet<EscalationSummary>("/escalation-summary"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load the escalation summary");
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
        Loading the escalation queue…
      </div>
    );
  }
  if (error || !d) return null;

  const res = d.resolution;

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <h3 className="text-lg font-semibold">Queue health &amp; resolution</h3>
        <div className={`rounded-xl border px-4 py-2 text-sm ${toneClasses(d.tone)}`}>{d.verdict}</div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Open now" value={d.open} bad={d.urgent_open > 0} />
        <Stat label="High-priority" value={d.urgent_open} bad={d.urgent_open > 0} />
        <Stat label="Awaiting approval" value={d.pending_approval} warn={d.pending_approval > 0} />
        <Stat
          label="Oldest open"
          value={d.oldest_open_days === null ? "—" : `${d.oldest_open_days}d`}
          warn={d.oldest_open_days !== null && d.oldest_open_days > d.stale_open_days}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* 30-day resolution scorecard */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Resolution — last {d.days} days</h4>
          <div className="grid grid-cols-2 gap-3">
            <Mini label="Raised" value={res.raised} />
            <Mini label="Resolved" value={res.resolved} />
            <Mini
              label="Resolution rate"
              value={res.resolution_rate === null ? "—" : `${res.resolution_rate}%`}
            />
            <Mini
              label="Avg time to close"
              value={res.avg_resolution_hours === null ? "—" : `${res.avg_resolution_hours}h`}
            />
          </div>
          {res.worst_resolution_hours !== null && (
            <p className="text-xs text-slate-500 mt-3">
              Slowest close in the window: {res.worst_resolution_hours}h
              {res.timed_resolutions < res.resolved &&
                ` · ${res.resolved - res.timed_resolutions} resolved without a timestamp (not timed)`}
            </p>
          )}
        </div>

        {/* Aging + the queue to work */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Work next (most severe, longest open)</h4>
          {d.queue.length === 0 ? (
            <p className="text-slate-500 text-sm">Queue clear — nothing open.</p>
          ) : (
            <ul className="space-y-2">
              {d.queue.slice(0, 5).map((q) => (
                <li key={q.id} className="flex items-center justify-between gap-3 text-sm">
                  <button
                    type="button"
                    onClick={() => onOpen?.(q.id)}
                    className="min-w-0 truncate text-left hover:underline"
                    title={q.title}
                  >
                    <span className="font-semibold">{q.title}</span>
                    <span className="text-slate-500">
                      {" "}· {q.machine || q.department || "—"}{q.agent_raised ? " · agent" : ""}
                    </span>
                  </button>
                  <span className={`whitespace-nowrap text-xs ${sevClasses(q.severity)}`}>
                    {q.severity} · {q.days_open}d open
                  </span>
                </li>
              ))}
            </ul>
          )}
          {d.aging.length > 0 && (
            <p className="text-xs text-slate-500 mt-3">
              Aging: {d.aging.map((a) => `${a.bucket}: ${a.count}`).join(" · ")}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

function Stat({ label, value, bad, warn }: { label: string; value: number | string; bad?: boolean; warn?: boolean }) {
  return (
    <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
      <p className="text-slate-400 text-xs">{label}</p>
      <h4 className={`text-2xl font-bold mt-1 ${bad ? "text-red-400" : warn ? "text-amber-400" : ""}`}>{value}</h4>
    </div>
  );
}

function Mini({ label, value }: { label: string; value: number | string }) {
  return (
    <div>
      <p className="text-slate-500 text-xs">{label}</p>
      <p className="text-lg font-bold">{value}</p>
    </div>
  );
}
