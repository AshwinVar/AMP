"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPost, errorDetail, getUserRole } from "../lib/api";

/**
 * Fleet Control — the live mission-control surface of the Fleet Ops pack.
 *
 * One poll (GET /fleet/state) drives everything on screen: the floor map
 * (stations derive from the tenant's own machine roster, so the plan IS the
 * plant), every vehicle's position/status/battery, the live task queue, and
 * the remote-assistance queue. The backend advances its lazy simulation on
 * each poll, so the 3-second interval below is also the world's frame rate.
 *
 * The assistance queue is deliberately advice-only (resume / return task /
 * send to charge) — mirroring how real robotaxi operators run remote help:
 * the human answers a vehicle's plea, the human never drives.
 */

type Station = { x: number; y: number; kind: string; machine_status?: string };
type Vehicle = {
  id: number; name: string; vehicle_type: string; status: string;
  x: number; y: number; battery_pct: number; current_task_id: number | null;
  odometer_m: number; deliveries_total: number;
};
type Task = {
  id: number; origin: string; destination: string; payload: string;
  priority: number; status: string; vehicle_id: number | null; source: string;
};
type FleetAlert = {
  id: number; vehicle_id: number; task_id: number | null; kind: string;
  severity: string; message: string; status: string;
};
type Kpis = {
  fleet_size: number; by_status: Record<string, number>;
  pending_tasks: number; active_tasks: number; delivered_24h: number;
  avg_wait_s: number | null; open_alerts: number;
  avg_battery_pct: number | null; distance_total_m: number;
};
type FleetState = {
  stations: Record<string, Station>;
  vehicles: Vehicle[];
  tasks: Task[];
  alerts: FleetAlert[];
  kpis: Kpis;
};

const VEHICLE_COLOR: Record<string, string> = {
  Idle: "#94a3b8",        // slate-400
  ToPickup: "#38bdf8",    // sky-400
  Carrying: "#34d399",    // emerald-400
  Charging: "#fbbf24",    // amber-400
  Stuck: "#f87171",       // red-400
  Offline: "#475569",     // slate-600
};

const STATUS_BADGE: Record<string, string> = {
  Idle: "border-slate-500/40 bg-slate-500/10 text-slate-300",
  ToPickup: "border-sky-500/40 bg-sky-500/10 text-sky-300",
  Carrying: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  Charging: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  Stuck: "border-red-500/40 bg-red-500/10 text-red-300",
  Offline: "border-slate-600/40 bg-slate-600/10 text-slate-400",
};

export default function FleetOpsSection() {
  const [state, setState] = useState<FleetState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const loadedOnce = useRef(false);

  const role = getUserRole();
  const isAdmin = role === "Admin";
  const canAdvise = role === "Admin" || role === "Supervisor";

  const load = useCallback(async () => {
    try {
      setState(await apiGet<FleetState>("/fleet/state"));
      setError(null);
      loadedOnce.current = true;
    } catch (e) {
      // Keep the last good frame on a blip; say what the server said.
      if (!loadedOnce.current) setError(errorDetail(e));
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 3000); // the world's frame rate
    return () => clearInterval(id);
  }, [load]);

  const seed = useCallback(async () => {
    setBusy(true);
    try {
      await apiPost("/fleet/seed", {});
      await load();
    } catch (e) {
      setError(errorDetail(e));
    } finally {
      setBusy(false);
    }
  }, [load]);

  const advise = useCallback(async (alertId: number, advice: string) => {
    try {
      await apiPost(`/fleet/alerts/${alertId}/advise`, { advice });
      await load();
    } catch (e) {
      // Reload even on refusal: an alert someone else resolved must leave.
      await load();
      setError(errorDetail(e));
    }
  }, [load]);

  const vehicles = state?.vehicles ?? [];
  const stations = state?.stations ?? {};
  const alerts = state?.alerts ?? [];
  const kpis = state?.kpis;
  const selected = vehicles.find((v) => v.id === selectedId) ?? null;
  const taskById = new Map((state?.tasks ?? []).map((t) => [t.id, t]));

  return (
    <section className="mt-8 space-y-6">
      <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold">Fleet Control</h2>
          <p className="text-slate-400 mt-2">
            Every mover on the floor, live — where it is, what it carries, and the
            pleas that need a human. Stations mirror this plant&apos;s own machine roster.
          </p>
        </div>
        {isAdmin && vehicles.length === 0 && state !== null && (
          <button onClick={seed} disabled={busy}
            className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-3 disabled:opacity-50">
            Seed demo fleet
          </button>
        )}
      </div>

      {kpis && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          <Kpi title="Fleet" value={kpis.fleet_size} />
          <Kpi title="Active tasks" value={kpis.active_tasks} />
          <Kpi title="Delivered (24h)" value={kpis.delivered_24h} />
          <Kpi title="Open pleas" value={kpis.open_alerts}
            tone={kpis.open_alerts > 0 ? "alert" : undefined} />
          <Kpi title="Avg battery"
            value={kpis.avg_battery_pct != null ? `${kpis.avg_battery_pct}%` : "—"} />
        </div>
      )}

      {error && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-4">{error}</div>
      )}

      {state === null && !error ? (
        <p className="text-slate-400">Contacting the fleet…</p>
      ) : vehicles.length === 0 && state !== null ? (
        <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-10 text-center">
          <div className="text-3xl mb-3">⬢</div>
          <h3 className="text-lg font-semibold">No vehicles yet</h3>
          <p className="text-slate-400 mt-1 text-sm">
            Register vehicles from your AMR/AGV vendor&apos;s API, or seed a simulated
            six-vehicle fleet to see the dispatch loop run on this plant&apos;s real layout.
          </p>
        </div>
      ) : (
        <>
          <div className="grid lg:grid-cols-3 gap-6">
            {/* ── The floor map ───────────────────────────── */}
            <div className="lg:col-span-2 rounded-2xl bg-slate-900 border border-slate-800 p-4">
              <svg viewBox="0 0 100 100" role="img" aria-label="Live fleet floor map"
                className="w-full rounded-xl bg-slate-950" style={{ maxHeight: 520 }}>
                {/* subtle grid */}
                {[20, 40, 60, 80].map((g) => (
                  <g key={g} stroke="#1e293b" strokeWidth="0.15">
                    <line x1={g} y1="0" x2={g} y2="100" />
                    <line x1="0" y1={g} x2="100" y2={g} />
                  </g>
                ))}
                {/* stations */}
                {Object.entries(stations).map(([name, s]) => (
                  <g key={name}>
                    {s.kind === "machine" ? (
                      <rect x={s.x - 2.6} y={s.y - 2.0} width="5.2" height="4.0" rx="0.6"
                        fill={s.machine_status === "Running" ? "#065f46"
                          : s.machine_status === "Breakdown" ? "#7f1d1d" : "#1e293b"}
                        stroke="#334155" strokeWidth="0.25" />
                    ) : (
                      <rect x={s.x - 3.4} y={s.y - 2.4} width="6.8" height="4.8" rx="1"
                        fill="#0c4a6e" opacity="0.55" stroke="#0ea5e9" strokeWidth="0.25" />
                    )}
                    <text x={s.x} y={s.y + 5.4} textAnchor="middle" fontSize="2.1"
                      fill="#64748b">{name}</text>
                  </g>
                ))}
                {/* intent lines: vehicle → its current target */}
                {vehicles.map((v) => {
                  const task = v.current_task_id ? taskById.get(v.current_task_id) : null;
                  if (!task) return null;
                  const targetName = v.status === "ToPickup" ? task.origin : task.destination;
                  const t = stations[targetName];
                  if (!t) return null;
                  return (
                    <line key={`l${v.id}`} x1={v.x} y1={v.y} x2={t.x} y2={t.y}
                      stroke={VEHICLE_COLOR[v.status] ?? "#64748b"} strokeWidth="0.3"
                      strokeDasharray="1.2 1.2" opacity="0.6" />
                  );
                })}
                {/* vehicles */}
                {vehicles.map((v) => (
                  <g key={v.id} onClick={() => setSelectedId(v.id === selectedId ? null : v.id)}
                    style={{ cursor: "pointer" }}>
                    <circle cx={v.x} cy={v.y} r={selectedId === v.id ? 2.2 : 1.7}
                      fill={VEHICLE_COLOR[v.status] ?? "#94a3b8"}
                      stroke="#020617" strokeWidth="0.35" />
                    {v.status === "Stuck" && (
                      <circle cx={v.x} cy={v.y} r="3.2" fill="none" stroke="#f87171"
                        strokeWidth="0.3" opacity="0.7" />
                    )}
                    <text x={v.x} y={v.y - 2.6} textAnchor="middle" fontSize="2"
                      fill="#cbd5e1" fontWeight="bold">{v.name}</text>
                  </g>
                ))}
              </svg>
              <div className="flex flex-wrap gap-3 mt-3 text-xs text-slate-400">
                {Object.entries(VEHICLE_COLOR).map(([s, c]) => (
                  <span key={s} className="flex items-center gap-1.5">
                    <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ background: c }} />
                    {s}{kpis?.by_status?.[s] ? ` (${kpis.by_status[s]})` : ""}
                  </span>
                ))}
              </div>
            </div>

            {/* ── Assistance queue ────────────────────────── */}
            <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 space-y-3">
              <h3 className="text-lg font-semibold">Assistance queue</h3>
              <p className="text-slate-500 text-sm">
                Vehicles that asked for help. Advice only — nobody drives remotely.
              </p>
              {alerts.length === 0 ? (
                <div className="rounded-xl border border-slate-800 bg-slate-950/60 p-6 text-center">
                  <div className="text-2xl mb-1">✓</div>
                  <p className="text-slate-400 text-sm">No open pleas. The fleet is coping.</p>
                </div>
              ) : (
                alerts.map((a) => (
                  <div key={a.id}
                    className="rounded-xl border border-red-500/30 bg-red-500/5 p-4 space-y-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="rounded-full px-2.5 py-0.5 text-xs border border-red-500/40 bg-red-500/10 text-red-300">
                        {a.kind}
                      </span>
                      <span className="text-xs text-slate-500">{a.severity}</span>
                    </div>
                    <p className="text-sm text-slate-300">{a.message}</p>
                    {canAdvise ? (
                      <div className="flex flex-wrap gap-2 pt-1">
                        <button onClick={() => advise(a.id, "resume")}
                          className="rounded-lg bg-emerald-500/90 text-slate-950 font-semibold px-3 py-1.5 text-xs hover:bg-emerald-400">
                          Resume
                        </button>
                        <button onClick={() => advise(a.id, "return_task")}
                          className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800">
                          Return task
                        </button>
                        <button onClick={() => advise(a.id, "send_to_charge")}
                          className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800">
                          Send to charge
                        </button>
                      </div>
                    ) : (
                      <p role="note" className="text-xs text-slate-500">
                        A Supervisor or Admin answers pleas.
                      </p>
                    )}
                  </div>
                ))
              )}

              {selected && (
                <div className="rounded-xl border border-slate-700 bg-slate-950/60 p-4 space-y-1">
                  <div className="flex items-center justify-between">
                    <h4 className="font-semibold">{selected.name}</h4>
                    <span className={`rounded-full px-2.5 py-0.5 text-xs border ${STATUS_BADGE[selected.status] ?? STATUS_BADGE.Idle}`}>
                      {selected.status}
                    </span>
                  </div>
                  <p className="text-sm text-slate-400">{selected.vehicle_type}</p>
                  <p className="text-sm text-slate-300">Battery {selected.battery_pct}%</p>
                  <p className="text-sm text-slate-300">
                    {selected.deliveries_total} deliveries · {(selected.odometer_m / 1000).toFixed(2)} km
                  </p>
                  {selected.current_task_id && taskById.get(selected.current_task_id) && (
                    <p className="text-sm text-slate-300">
                      Task #{selected.current_task_id}: {taskById.get(selected.current_task_id)!.origin}
                      {" to "}{taskById.get(selected.current_task_id)!.destination}
                    </p>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* ── Roster ─────────────────────────────────────── */}
          <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 overflow-x-auto">
            <h3 className="text-lg font-semibold mb-3">Fleet roster</h3>
            <table className="w-full text-sm min-w-[560px]">
              <thead>
                <tr className="text-left text-slate-500 border-b border-slate-800">
                  <th className="py-2 pr-4">Vehicle</th>
                  <th className="py-2 pr-4">Type</th>
                  <th className="py-2 pr-4">Status</th>
                  <th className="py-2 pr-4">Battery</th>
                  <th className="py-2 pr-4">Deliveries</th>
                  <th className="py-2">Distance</th>
                </tr>
              </thead>
              <tbody>
                {vehicles.map((v) => (
                  <tr key={v.id} className="border-b border-slate-800/60 last:border-0">
                    <td className="py-2 pr-4 font-semibold">{v.name}</td>
                    <td className="py-2 pr-4 text-slate-400">{v.vehicle_type}</td>
                    <td className="py-2 pr-4">
                      <span className={`rounded-full px-2.5 py-0.5 text-xs border ${STATUS_BADGE[v.status] ?? STATUS_BADGE.Idle}`}>
                        {v.status}
                      </span>
                    </td>
                    <td className="py-2 pr-4">
                      <span className={v.battery_pct < 20 ? "text-red-300" : v.battery_pct < 40 ? "text-amber-300" : "text-slate-300"}>
                        {v.battery_pct}%
                      </span>
                    </td>
                    <td className="py-2 pr-4 text-slate-300">{v.deliveries_total}</td>
                    <td className="py-2 text-slate-300">{(v.odometer_m / 1000).toFixed(2)} km</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

function Kpi({ title, value, tone }: { title: string; value: string | number; tone?: "alert" }) {
  return (
    <div className={`rounded-2xl bg-slate-900 border p-5 ${tone === "alert" ? "border-red-500/40" : "border-slate-800"}`}>
      <p className="text-slate-400 text-sm">{title}</p>
      <h3 className={`text-2xl font-bold mt-2 ${tone === "alert" ? "text-red-300" : ""}`}>{value}</h3>
    </div>
  );
}
