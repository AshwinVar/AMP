"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import ConnectionDrawer from "./ConnectionDrawer";

// Mirrors the backend connectivity read-model (ai/connectivity.py build_connectivity_summary).
type ByMachine = {
  machine_id: number; name: string; line: string;
  state: "fresh" | "stale" | "dark";
  last_signal_minutes: number | null;
  last_signal_at: string | null;
  signals: number;
  linked: boolean;
};
type OfflineDevice = {
  device_code: string; device_name: string; device_type: string;
  protocol: string; status: string; linked_machine: string | null;
};
type ConnectivitySummary = {
  stale_after_minutes: number;
  lookback_hours: number;
  machines_tracked: number;
  reporting: number;
  fresh: number;
  stale: number;
  dark: number;
  connectivity_score: number;
  devices: {
    total: number; online: number; offline: number; online_rate: number;
    by_protocol: { protocol: string; count: number }[];
    by_status: { status: string; count: number }[];
  };
  signal_quality: { total: number; good: number; bad: number; good_rate: number };
  instrumentation: { linked: number; coverage: number };
  by_machine: ByMachine[];
  needs_attention: ByMachine | null;
  offline_devices: OfflineDevice[];
};

function scoreColor(pct: number) {
  if (pct >= 95) return "text-emerald-400";
  if (pct >= 80) return "text-yellow-400";
  if (pct >= 60) return "text-orange-400";
  return "text-red-400";
}

const STATE_STYLE: Record<ByMachine["state"], { label: string; dot: string; text: string }> = {
  fresh: { label: "Fresh", dot: "bg-emerald-400", text: "text-emerald-400" },
  stale: { label: "Stale", dot: "bg-orange-400", text: "text-orange-400" },
  dark: { label: "Dark", dot: "bg-red-500", text: "text-red-400" },
};

// "how long ago" for a last-signal age in minutes.
function ago(mins: number | null) {
  if (mins == null) return "no recent signal";
  if (mins < 60) return `${Math.round(mins)}m ago`;
  if (mins < 60 * 24) return `${(mins / 60).toFixed(1)}h ago`;
  return `${(mins / 60 / 24).toFixed(1)}d ago`;
}

// A glanceable read-out of whether the OT edge is actually alive — the
// connectivity score, how many machines are fresh / stale / dark, the device
// online and signal-quality rates, and the silent connections to chase.
// Self-contained: fetches its own summary and refreshes. Renders nothing until
// there are machines to track. Each machine (and the connection to fix first)
// opens the drill-down drawer; the offline devices jump to the connectivity
// console, where the estate is managed.
export default function ConnectivitySnapshot({ onOpen }: { onOpen?: (viewKey: string) => void }) {
  const [d, setD] = useState<ConnectivitySummary | null>(null);
  const [machineId, setMachineId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      setD(await apiGet<ConnectivitySummary>("/connectivity-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!d || d.machines_tracked === 0) return null;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Edge connectivity</h3>
          <p className="text-slate-400 text-sm mt-1">
            {d.fresh} of {d.machines_tracked} machine{d.machines_tracked !== 1 ? "s" : ""} fresh (&lt; {d.stale_after_minutes}m) · {d.stale} stale · {d.dark} dark
          </p>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${scoreColor(d.connectivity_score)}`}>{d.connectivity_score}%</p>
          <p className="text-[11px] text-slate-500">connected</p>
        </div>
      </div>

      {/* state + device + signal KPIs */}
      <div className="mt-4 grid grid-cols-2 md:grid-cols-4 gap-2">
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className="text-xl font-bold text-emerald-300">{d.fresh}</p>
          <p className="text-[11px] text-slate-500">fresh</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className="text-xl font-bold text-orange-300">{d.stale}</p>
          <p className="text-[11px] text-slate-500">stale</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className="text-xl font-bold text-red-300">{d.dark}</p>
          <p className="text-[11px] text-slate-500">dark</p>
        </div>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
          <p className="text-xl font-bold text-sky-300">{d.devices.online}/{d.devices.total}</p>
          <p className="text-[11px] text-slate-500">devices online</p>
        </div>
      </div>

      {d.needs_attention && (
        <button
          type="button"
          onClick={() => setMachineId(d.needs_attention!.machine_id)}
          title={`${d.needs_attention.name} — click for connection detail`}
          className="mt-4 w-full text-left rounded-lg border border-slate-800 border-l-2 border-l-red-500/70 bg-slate-900/40 px-3 py-2 hover:border-slate-600 hover:bg-slate-800/60 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
        >
          <p className="text-[11px] uppercase tracking-wide text-slate-500">Fix this connection first</p>
          <p className="text-sm text-slate-200 mt-0.5">
            <span className="font-medium">{d.needs_attention.name}</span>
            <span className="text-slate-500">
              {" · "}{STATE_STYLE[d.needs_attention.state].label.toLowerCase()} · {ago(d.needs_attention.last_signal_minutes)}
              {!d.needs_attention.linked ? " · no device linked" : ""}
            </span>
          </p>
        </button>
      )}

      <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* silent / degraded machines */}
        <div>
          <p className="text-xs text-slate-500 mb-2">Reporting status</p>
          <div className="space-y-2">
            {d.by_machine.map((m) => {
              const st = STATE_STYLE[m.state];
              return (
                <button
                  key={m.machine_id}
                  type="button"
                  onClick={() => setMachineId(m.machine_id)}
                  title={`${m.name} — click for connection detail`}
                  className="w-full flex items-center justify-between rounded-lg border border-slate-800 px-3 py-2 text-sm hover:border-slate-600 hover:bg-slate-800/60 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
                >
                  <div className="min-w-0 flex-1 text-left flex items-center gap-2">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${st.dot}`} />
                    <div className="min-w-0">
                      <p className="text-slate-200 font-medium truncate">{m.name}</p>
                      <p className="text-[11px] text-slate-500">
                        {ago(m.last_signal_minutes)}{m.signals ? ` · ${m.signals} signal${m.signals !== 1 ? "s" : ""}/${d.lookback_hours}h` : ""}
                      </p>
                    </div>
                  </div>
                  <span className={`tabular-nums shrink-0 text-[11px] uppercase ${st.text}`}>{st.label}</span>
                </button>
              );
            })}
          </div>
        </div>

        {/* estate health: signal quality, coverage, offline devices */}
        <div>
          <p className="text-xs text-slate-500 mb-2">Estate health</p>
          <div className="grid grid-cols-2 gap-2">
            <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
              <p className={`text-lg font-bold ${scoreColor(d.signal_quality.good_rate)}`}>{d.signal_quality.good_rate}%</p>
              <p className="text-[11px] text-slate-500">signal quality</p>
            </div>
            <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-3 py-2 text-center">
              <p className="text-lg font-bold text-slate-200">{d.instrumentation.coverage}%</p>
              <p className="text-[11px] text-slate-500">instrumented</p>
            </div>
          </div>

          <p className="text-xs text-slate-500 mt-4 mb-2">Offline devices</p>
          {d.offline_devices.length === 0 ? (
            <p className="text-emerald-400 text-sm">Every registered device is online.</p>
          ) : (
            <div className="space-y-2">
              {d.offline_devices.map((dev) => {
                const cls = "flex items-center justify-between rounded-lg border border-slate-800 px-3 py-2 text-sm";
                const inner = (
                  <>
                    <div className="min-w-0 flex-1 text-left">
                      <p className="text-slate-200 font-medium truncate">{dev.device_name}</p>
                      <p className="text-[11px] text-slate-500 truncate">
                        {dev.device_type} · {dev.protocol}{dev.linked_machine ? ` · ${dev.linked_machine}` : ""}
                      </p>
                    </div>
                    <span className="tabular-nums shrink-0 text-[11px] uppercase text-red-400">{dev.status}</span>
                  </>
                );
                return onOpen ? (
                  <button
                    key={dev.device_code}
                    type="button"
                    onClick={() => onOpen("connectivity")}
                    title="Open the connectivity console"
                    className={`${cls} w-full hover:border-slate-600 hover:bg-slate-800/60 transition focus:outline-none focus:ring-2 focus:ring-slate-600`}
                  >
                    {inner}
                  </button>
                ) : (
                  <div key={dev.device_code} className={cls}>{inner}</div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {machineId !== null && (
        <ConnectionDrawer machineId={machineId} onClose={() => setMachineId(null)} />
      )}
    </div>
  );
}
