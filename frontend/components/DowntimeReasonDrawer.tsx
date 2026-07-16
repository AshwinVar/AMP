"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend reason drill-down (ai/downtime.py build_downtime_reason).
type Instance = {
  id: number;
  machine_id: number | null;
  machine: string;
  duration: string;
  minutes: number;
  notes: string | null;
  at: string | null;
};

type ReasonDetail = {
  reason: string;
  days: number;
  total_events: number;
  total_minutes: number;
  by_machine: { machine_id: number; name: string; count: number; minutes: number }[];
  daily: { date: string; count: number }[];
  instances: Instance[];
};

function mins(n: number) {
  if (!n) return "0m";
  const h = Math.floor(n / 60);
  const m = n % 60;
  return h ? `${h}h ${m}m` : `${m}m`;
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

export default function DowntimeReasonDrawer({ reason, onClose }: { reason: string; onClose: () => void }) {
  const [detail, setDetail] = useState<ReasonDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDetail(await apiGet<ReasonDetail>(`/downtime-reason?reason=${encodeURIComponent(reason)}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load downtime detail");
    } finally {
      setLoading(false);
    }
  }, [reason]);

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

  const peak = detail ? detail.daily.reduce((m, d) => Math.max(m, d.count), 0) : 0;

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
            <h2 className="text-2xl font-bold">Downtime — {reason}</h2>
            <p className="text-slate-500 text-sm mt-1">Where this reason is costing time · last 7 days</p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl px-2" aria-label="Close">
            ✕
          </button>
        </div>

        {error && (
          <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-3 text-sm">{error}</div>
        )}

        {loading && !detail ? (
          <p className="text-slate-400 mt-6">Loading downtime detail…</p>
        ) : detail ? (
          <div className="mt-5 space-y-6">
            {/* Totals */}
            <div className="grid grid-cols-2 gap-3">
              <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                <p className="text-3xl font-bold text-red-400">{detail.total_events}</p>
                <p className="text-xs text-slate-500 mt-1">event{detail.total_events !== 1 ? "s" : ""}</p>
              </div>
              <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                <p className="text-3xl font-bold text-orange-300">{mins(detail.total_minutes)}</p>
                <p className="text-xs text-slate-500 mt-1">time lost</p>
              </div>
            </div>

            {detail.total_events === 0 ? (
              <p className="text-slate-500 text-sm">No “{reason}” downtime in the last 7 days.</p>
            ) : (
              <>
                {/* 7-day trend */}
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Trend · last 7 days</h3>
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
                          <div className="w-full bg-red-500/60 rounded-t" style={{ height: `${h}px` }} />
                          <span className="text-[10px] text-slate-500">{wk(d.date)}</span>
                        </div>
                      );
                    })}
                  </div>
                </div>

                {/* Machines hit */}
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Machines hit</h3>
                  <div className="mt-3 space-y-2">
                    {detail.by_machine.map((m) => (
                      <div
                        key={m.machine_id}
                        className="flex items-center justify-between rounded-lg border border-slate-800 bg-slate-900 px-3 py-2 text-sm"
                      >
                        <span className="text-slate-300">{m.name}</span>
                        <span className="text-slate-500">
                          {m.count} event{m.count !== 1 ? "s" : ""} · {mins(m.minutes)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Recent instances */}
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Recent instances</h3>
                  <ol className="mt-3 space-y-3">
                    {detail.instances.map((i) => (
                      <li key={i.id} className="border-b border-slate-800/70 pb-3">
                        <div className="flex items-center justify-between gap-2">
                          <p className="text-sm font-medium">{i.machine}</p>
                          <span className="text-xs text-slate-400">{i.duration}</span>
                        </div>
                        {i.notes && <p className="text-xs text-slate-400 mt-0.5">{i.notes}</p>}
                        <p className="text-xs text-slate-600 mt-0.5">{fmt(i.at)}</p>
                      </li>
                    ))}
                  </ol>
                </div>
              </>
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}
