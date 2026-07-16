"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";

// Mirrors the backend detail read-model (ai/twin.py build_machine_detail).
type OpenAction = {
  id: number;
  agent: string;
  action_type: string;
  summary: string;
  severity: string;
  created_at: string | null;
};

type TimelineEvent = {
  kind: string;
  at: string | null;
  title: string;
  detail: string;
  status: string | null;
};

type Detail = {
  machine_id: number;
  name: string;
  status: string;
  utilization: number;
  downtime: string;
  health_score: number;
  health_band: string;
  risk_score: number;
  risk_level: string;
  risk_factors: string[];
  downtime_7d: { date: string; count: number }[];
  production_7d: { good: number; total: number; good_rate: number; daily: { date: string; count: number }[] };
  quality: { inspections: number; inspected: number; passed: number; failed: number; fail_rate: number; top_defects: { category: string; count: number }[] };
  open_actions: OpenAction[];
  timeline: TimelineEvent[];
};

function healthColor(score: number) {
  if (score >= 80) return "text-emerald-400";
  if (score >= 55) return "text-yellow-400";
  if (score >= 35) return "text-orange-400";
  return "text-red-400";
}

function bandStyle(band: string) {
  if (band === "Healthy") return "border-emerald-500/40 bg-emerald-500/10 text-emerald-300";
  if (band === "Watch") return "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
  if (band === "At risk") return "border-orange-500/40 bg-orange-500/10 text-orange-300";
  return "border-red-500/40 bg-red-500/10 text-red-300"; // Critical
}

function kindIcon(kind: string) {
  if (kind === "downtime") return "◷";
  if (kind === "task") return "✚";
  if (kind === "action") return "⊙";
  return "•";
}

function kindColor(kind: string) {
  if (kind === "downtime") return "text-red-400";
  if (kind === "task") return "text-indigo-400";
  if (kind === "action") return "text-amber-400";
  return "text-slate-400";
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

function DowntimeSparkline({ series }: { series: { date: string; count: number }[] }) {
  const peak = series.reduce((m, d) => Math.max(m, d.count), 0);
  const total = series.reduce((s, d) => s + d.count, 0);
  return (
    <div>
      <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Downtime · last 7 days</h3>
      {total === 0 ? (
        <p className="text-slate-500 text-sm mt-2">No downtime in the last 7 days.</p>
      ) : (
        <div className="mt-3 flex items-end gap-2 h-20">
          {series.map((d) => {
            const h = peak ? Math.max(4, Math.round((d.count / peak) * 72)) : 4;
            return (
              <div
                key={d.date}
                className="flex-1 flex flex-col items-center justify-end gap-1"
                title={`${d.count} on ${d.date}`}
              >
                <span className="text-[10px] text-slate-400">{d.count || ""}</span>
                <div className="w-full bg-red-500/60 rounded-t" style={{ height: `${h}px` }} />
                <span className="text-[10px] text-slate-500">{wk(d.date)}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function MachineDetailDrawer({
  machineId,
  onClose,
  onChanged,
}: {
  machineId: number;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDetail(await apiGet<Detail>(`/machine-health/${machineId}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load machine detail");
    } finally {
      setLoading(false);
    }
  }, [machineId]);

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
        onChanged(); // and the list behind it
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to update the action");
      }
    },
    [load, onChanged],
  );

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
            <h2 className="text-2xl font-bold">{detail?.name ?? "Machine"}</h2>
            <p className="text-slate-500 text-sm mt-1">Machine cockpit — health, risk, and history</p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl px-2" aria-label="Close">
            ✕
          </button>
        </div>

        {error && (
          <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-3 text-sm">{error}</div>
        )}

        {loading && !detail ? (
          <p className="text-slate-400 mt-6">Loading machine detail…</p>
        ) : detail ? (
          <div className="mt-5 space-y-6">
            {/* Health header */}
            <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
              <div className="flex items-end gap-4">
                <div>
                  <p className={`text-5xl font-bold ${healthColor(detail.health_score)}`}>{detail.health_score}</p>
                  <p className="text-xs text-slate-500">health score</p>
                </div>
                <div className="flex-1 text-sm text-slate-400 space-y-0.5 pb-1">
                  <p>
                    {detail.status} · util {detail.utilization}%
                  </p>
                  <p>
                    risk {detail.risk_level} ({detail.risk_score})
                  </p>
                </div>
                <span className={`rounded-full px-3 py-1 text-xs border ${bandStyle(detail.health_band)}`}>
                  {detail.health_band}
                </span>
              </div>
            </div>

            {/* Risk factors */}
            <div>
              <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Risk factors</h3>
              {detail.risk_factors.length === 0 ? (
                <p className="text-slate-500 text-sm mt-2">No major risk indicators.</p>
              ) : (
                <ul className="mt-2 space-y-1.5">
                  {detail.risk_factors.map((r, i) => (
                    <li key={i} className="flex gap-2 text-sm text-slate-300">
                      <span className="text-orange-400">▹</span>
                      <span>{r}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* 7-day downtime sparkline */}
            <DowntimeSparkline series={detail.downtime_7d} />

            {/* Production (last 7 days) */}
            <div>
              <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Production · last 7 days</h3>
              {detail.production_7d.total === 0 ? (
                <p className="text-slate-500 text-sm mt-2">No production recorded this week.</p>
              ) : (
                <>
                  <div className="flex items-end gap-3 mt-2">
                    <p className={`text-2xl font-bold ${detail.production_7d.good_rate >= 95 ? "text-emerald-400" : detail.production_7d.good_rate >= 90 ? "text-yellow-400" : "text-orange-400"}`}>
                      {detail.production_7d.good_rate}%
                    </p>
                    <p className="text-xs text-slate-500 pb-1">
                      good rate · {detail.production_7d.good.toLocaleString()} / {detail.production_7d.total.toLocaleString()} units
                    </p>
                  </div>
                  <div className="mt-3 flex items-end gap-2 h-16">
                    {detail.production_7d.daily.map((d) => {
                      const peak = Math.max(1, ...detail.production_7d.daily.map((x) => x.count));
                      const h = Math.max(4, Math.round((d.count / peak) * 56));
                      return (
                        <div key={d.date} className="flex-1 flex flex-col items-center justify-end gap-1" title={`${d.count} on ${d.date}`}>
                          <div className="w-full bg-emerald-500/60 rounded-t" style={{ height: `${h}px` }} />
                          <span className="text-[10px] text-slate-500">{wk(d.date)}</span>
                        </div>
                      );
                    })}
                  </div>
                </>
              )}
            </div>

            {/* Quality */}
            <div>
              <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Quality</h3>
              {detail.quality.inspections === 0 ? (
                <p className="text-slate-500 text-sm mt-2">No inspections for this machine.</p>
              ) : (
                <div className="mt-2">
                  <div className="flex items-end gap-3">
                    <p className={`text-2xl font-bold ${detail.quality.fail_rate <= 2 ? "text-emerald-400" : detail.quality.fail_rate <= 5 ? "text-yellow-400" : "text-orange-400"}`}>
                      {detail.quality.fail_rate}%
                    </p>
                    <p className="text-xs text-slate-500 pb-1">
                      fail rate · {detail.quality.inspections} inspection{detail.quality.inspections !== 1 ? "s" : ""} · {detail.quality.inspected} units
                    </p>
                  </div>
                  {detail.quality.top_defects.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {detail.quality.top_defects.map((d) => (
                        <span key={d.category} className="rounded-lg border border-slate-700 px-2.5 py-1 text-xs text-slate-300">
                          {d.category} <span className="text-slate-500">· {d.count}</span>
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* Pending agent actions — approve/reject inline */}
            {detail.open_actions.length > 0 && (
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Awaiting approval</h3>
                <div className="mt-2 space-y-3">
                  {detail.open_actions.map((a) => (
                    <div key={a.id} className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4">
                      <div className="flex items-center gap-2 text-xs text-slate-400">
                        <span className="rounded-full border border-slate-600/40 bg-slate-500/15 text-slate-300 px-2 py-0.5">
                          {a.agent} agent
                        </span>
                        <span>{a.action_type}</span>
                      </div>
                      <p className="text-sm font-medium mt-2">{a.summary}</p>
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
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Unified timeline */}
            <div>
              <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Timeline</h3>
              {detail.timeline.length === 0 ? (
                <p className="text-slate-500 text-sm mt-2">Nothing has happened to this machine yet.</p>
              ) : (
                <ol className="mt-3 space-y-3">
                  {detail.timeline.map((e, i) => (
                    <li key={i} className="flex gap-3">
                      <span className={`text-lg leading-none ${kindColor(e.kind)}`}>{kindIcon(e.kind)}</span>
                      <div className="flex-1 border-b border-slate-800/70 pb-3">
                        <div className="flex items-center justify-between gap-2">
                          <p className="text-sm font-medium">{e.title}</p>
                          {e.status && <span className="text-xs text-slate-500">{e.status}</span>}
                        </div>
                        {e.detail && <p className="text-xs text-slate-400 mt-0.5">{e.detail}</p>}
                        <p className="text-xs text-slate-600 mt-0.5">{fmt(e.at)}</p>
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
