"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend edge-connection drill-down (ai/connectivity.py build_connection_detail).
type Tag = {
  signal_name: string; reads: number;
  last_value: string; unit: string | null; source: string | null;
  last_at: string; silent_minutes: number;
  cadence_minutes: number | null; dropped: boolean;
};
type Device = {
  device_code: string; device_name: string; device_type: string;
  protocol: string; ip_address: string | null; topic: string | null;
  status: string; online: boolean;
  signals: number; bad_signals: number; last_signal_at: string | null;
};
type BlindSpot = { severity: "high" | "medium"; message: string };
type Order = {
  work_order_no: string; part_number: string; status: string;
  target: number; actual: number;
};

type ConnectionDetail = {
  found: boolean;
  machine_id: number;
  name: string | null;
  line: string;
  status: string | null;
  stale_after_minutes: number;
  lookback_hours: number;
  state: "fresh" | "stale" | "dark";
  last_signal_at: string | null;
  last_signal_minutes: number | null;
  signals: number;
  cadence_minutes: number | null;
  overdue_multiple: number | null;
  by_signal: Tag[];
  dropped_signals: number;
  devices: Device[];
  linked: boolean;
  signal_quality: { total: number; good: number; bad: number; good_rate: number };
  open_work_orders: { count: number; orders: Order[] };
  blind_spots: BlindSpot[];
  recent: { signal_name: string; signal_value: string; unit: string | null; source: string | null; at: string }[];
};

const STATE: Record<ConnectionDetail["state"], { label: string; dot: string; text: string }> = {
  fresh: { label: "Fresh", dot: "bg-emerald-400", text: "text-emerald-400" },
  stale: { label: "Stale", dot: "bg-orange-400", text: "text-orange-400" },
  dark: { label: "Dark", dot: "bg-red-500", text: "text-red-400" },
};

// "how long ago" for an age in minutes — same phrasing as the summary card.
function ago(mins: number | null) {
  if (mins == null) return "no recent signal";
  if (mins < 60) return `${Math.round(mins)}m ago`;
  if (mins < 60 * 24) return `${(mins / 60).toFixed(1)}h ago`;
  return `${(mins / 60 / 24).toFixed(1)}d ago`;
}

const cadence = (m: number | null) => (m == null ? "—" : m < 60 ? `${m}m` : `${(m / 60).toFixed(1)}h`);

function qualityColor(pct: number) {
  if (pct >= 95) return "text-emerald-400";
  if (pct >= 80) return "text-yellow-400";
  return "text-red-400";
}

// A right-hand drawer that drills into one machine's edge connection: its state
// and last signal, the silence measured against its own reporting cadence, which
// signal tags have dropped, the devices wired to it, the read quality, the open
// work orders going unreported while it is quiet, and the blind spots that
// follow. Self-fetches from /connectivity-machine; closes on Escape.
export default function ConnectionDrawer({
  machineId,
  onClose,
}: {
  machineId: number;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<ConnectionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setDetail(await apiGet<ConnectionDetail>(`/connectivity-machine?machine_id=${machineId}`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load connection detail");
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

  const st = detail ? STATE[detail.state] : STATE.dark;

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
            <h2 className="text-2xl font-bold">{detail?.name ?? `Machine #${machineId}`}</h2>
            <p className="text-slate-500 text-sm mt-1">
              Edge connection · last {detail?.lookback_hours ?? 24}h
              {detail?.line ? ` · ${detail.line}` : ""}
              {detail?.status ? ` · reported ${detail.status}` : ""}
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white text-xl px-2" aria-label="Close">
            ✕
          </button>
        </div>

        {error && (
          <div className="mt-4 rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-3 text-sm">{error}</div>
        )}

        {loading && !detail ? (
          <p className="text-slate-400 mt-6">Loading connection detail…</p>
        ) : detail ? (
          !detail.found ? (
            <p className="text-slate-500 text-sm mt-6">No machine on record for #{machineId}.</p>
          ) : (
            <div className="mt-5 space-y-6">
              {/* Headline: connection state + how overdue the silence is */}
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                  <p className={`text-3xl font-bold ${st.text} flex items-center gap-2`}>
                    <span className={`h-3 w-3 rounded-full ${st.dot}`} />
                    {st.label}
                  </p>
                  <p className="text-xs text-slate-500 mt-1">
                    {ago(detail.last_signal_minutes)} · fresh &lt; {detail.stale_after_minutes}m
                  </p>
                </div>
                <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
                  <p className="text-3xl font-bold text-slate-200">
                    {detail.overdue_multiple != null ? `${detail.overdue_multiple}×` : "—"}
                  </p>
                  <p className="text-xs text-slate-500 mt-1">
                    past its own cadence{detail.cadence_minutes != null ? ` of ${cadence(detail.cadence_minutes)}` : ""}
                  </p>
                </div>
              </div>

              {/* Connection KPIs */}
              <div className="grid grid-cols-3 gap-2">
                <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
                  <p className="text-xl font-bold text-sky-300">{detail.signals}</p>
                  <p className="text-[11px] text-slate-500">reads · {detail.lookback_hours}h</p>
                </div>
                <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
                  <p className={`text-xl font-bold ${detail.dropped_signals ? "text-orange-300" : "text-slate-200"}`}>
                    {detail.dropped_signals}/{detail.by_signal.length}
                  </p>
                  <p className="text-[11px] text-slate-500">tags dropped</p>
                </div>
                <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
                  <p className={`text-xl font-bold ${qualityColor(detail.signal_quality.good_rate)}`}>
                    {detail.signal_quality.good_rate}%
                  </p>
                  <p className="text-[11px] text-slate-500">read quality</p>
                </div>
              </div>

              {/* What we're blind on */}
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                  What this silence hides
                </h3>
                {detail.blind_spots.length === 0 ? (
                  <p className="text-emerald-400 text-sm mt-3">
                    This connection is healthy — every tag is reporting and the reads are good.
                  </p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {detail.blind_spots.map((b, i) => (
                      <div
                        key={i}
                        className={`rounded-lg border border-slate-800 border-l-2 ${b.severity === "high" ? "border-l-red-500/70" : "border-l-amber-400/70"} bg-slate-900/40 px-3 py-2`}
                      >
                        <p className={`text-sm ${b.severity === "high" ? "text-slate-200" : "text-slate-300"}`}>
                          {b.message}
                        </p>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* Per-tag reporting */}
              {detail.by_signal.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Signal tags</h3>
                  <div className="mt-3 space-y-2">
                    {detail.by_signal.map((t) => (
                      <div
                        key={t.signal_name}
                        className={`flex items-center justify-between rounded-lg border border-slate-800 ${t.dropped ? "border-l-2 border-l-orange-400/70" : ""} px-3 py-2 text-sm`}
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-slate-200 font-medium truncate">
                            {t.signal_name}
                            <span className="text-slate-500 font-normal">
                              {" "}· {t.last_value}
                              {t.unit ? ` ${t.unit}` : ""}
                            </span>
                          </p>
                          <p className="text-[11px] text-slate-500">
                            {t.reads} read{t.reads !== 1 ? "s" : ""}
                            {t.cadence_minutes != null ? ` · every ${cadence(t.cadence_minutes)}` : ""}
                            {t.source ? ` · ${t.source}` : ""}
                          </p>
                        </div>
                        <span
                          className={`tabular-nums shrink-0 text-[11px] ${t.dropped ? "text-orange-400" : "text-slate-500"}`}
                        >
                          {ago(t.silent_minutes)}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* The edge estate wired to this machine */}
              <div>
                <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                  Linked devices · {detail.devices.length}
                </h3>
                {detail.devices.length === 0 ? (
                  <p className="text-slate-500 text-sm mt-3">
                    Nothing is wired to this machine — it has no registered edge device to report through.
                  </p>
                ) : (
                  <div className="mt-3 space-y-2">
                    {detail.devices.map((dv) => (
                      <div
                        key={dv.device_code}
                        className={`flex items-center justify-between rounded-lg border border-slate-800 border-l-2 ${dv.online ? "border-l-slate-700" : "border-l-red-500/70"} bg-slate-900/40 px-3 py-2 text-sm`}
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-slate-200 font-medium truncate">
                            {dv.device_name} <span className="text-slate-500">· {dv.device_code}</span>
                          </p>
                          <p className="text-[11px] text-slate-500 truncate">
                            {dv.device_type} · {dv.protocol}
                            {dv.ip_address ? ` · ${dv.ip_address}` : ""}
                            {dv.topic ? ` · ${dv.topic}` : ""}
                          </p>
                          <p className="text-[11px] text-slate-500">
                            {dv.signals} read{dv.signals !== 1 ? "s" : ""}
                            {dv.bad_signals ? ` · ${dv.bad_signals} bad` : ""}
                          </p>
                        </div>
                        <span
                          className={`tabular-nums shrink-0 text-[11px] uppercase ${dv.online ? "text-emerald-400" : "text-red-400"}`}
                        >
                          {dv.status}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* What the silence costs — jobs running unreported */}
              {detail.open_work_orders.count > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
                    Open work orders · {detail.open_work_orders.count}
                  </h3>
                  <div className="mt-3 space-y-2">
                    {detail.open_work_orders.orders.map((o) => (
                      <div
                        key={o.work_order_no}
                        className="flex items-center justify-between rounded-lg border border-slate-800 px-3 py-2 text-sm"
                      >
                        <div className="min-w-0 flex-1">
                          <p className="text-slate-200 font-medium truncate">
                            {o.work_order_no} <span className="text-slate-500">· {o.part_number}</span>
                          </p>
                          <p className="text-[11px] text-slate-500">{o.status}</p>
                        </div>
                        <span className="tabular-nums shrink-0 text-[11px] text-slate-400">
                          {o.actual}/{o.target}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Raw evidence */}
              {detail.recent.length > 0 && (
                <div>
                  <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">Last reads</h3>
                  <ol className="mt-3 space-y-3">
                    {detail.recent.map((r, i) => (
                      <li key={`${r.at}-${i}`} className="border-b border-slate-800/70 pb-3">
                        <div className="flex items-center justify-between gap-2">
                          <p className="text-sm font-medium truncate">{r.signal_name}</p>
                          <span className="text-xs text-sky-300 shrink-0 tabular-nums">
                            {r.signal_value}
                            {r.unit ? ` ${r.unit}` : ""}
                          </span>
                        </div>
                        <p className="text-xs text-slate-600 mt-0.5">
                          {new Date(r.at).toLocaleString()}
                          {r.source ? ` · ${r.source}` : ""}
                        </p>
                      </li>
                    ))}
                  </ol>
                </div>
              )}
            </div>
          )
        ) : null}
      </div>
    </div>
  );
}
