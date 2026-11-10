"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPost, errorDetail, getUserRole } from "../lib/api";

/**
 * Transport Tasks — the orchestration half of the Fleet Ops pack.
 *
 * A transport task is the unit the dispatch engine works from: "take this
 * payload from here to there". Two things raise one — a person on this
 * screen, or the plant itself (source "mes": a running machine finishing a
 * batch). The engine then assigns the nearest healthy idle vehicle, and the
 * row's timestamps tell the whole story: requested → assigned → picked up →
 * delivered.
 *
 * Origin/destination are this plant's REAL stations: the four logistics
 * anchors plus every machine in the roster, served by the same /fleet/state
 * the map draws from — so the form can never offer a place that doesn't exist.
 */

type Station = { x: number; y: number; kind: string };
type Task = {
  id: number; origin: string; destination: string; payload: string;
  priority: number; status: string; vehicle_id: number | null; source: string;
  requested_at: string | null; assigned_at: string | null;
  picked_up_at: string | null; completed_at: string | null;
};
type Kpis = {
  pending_tasks: number; active_tasks: number; delivered_24h: number;
  avg_wait_s: number | null;
};
type FleetState = {
  stations: Record<string, Station>;
  tasks: Task[];
  kpis: Kpis;
  vehicles: { id: number; name: string }[];
};

const PRIORITY_LABEL: Record<number, string> = { 3: "High", 2: "Normal", 1: "Low" };

const STATUS_BADGE: Record<string, string> = {
  Pending: "border-slate-500/40 bg-slate-500/10 text-slate-300",
  Assigned: "border-sky-500/40 bg-sky-500/10 text-sky-300",
  InTransit: "border-emerald-500/40 bg-emerald-500/10 text-emerald-300",
  Delivered: "border-emerald-600/40 bg-emerald-600/10 text-emerald-200",
  Cancelled: "border-slate-600/40 bg-slate-600/10 text-slate-400",
};

export default function TransportTasksSection() {
  const [state, setState] = useState<FleetState | null>(null);
  const [history, setHistory] = useState<Task[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [origin, setOrigin] = useState("");
  const [destination, setDestination] = useState("");
  const [payload, setPayload] = useState("");
  const [priority, setPriority] = useState(2);
  const loadedOnce = useRef(false);

  const role = getUserRole();
  const canCreate = ["Admin", "Supervisor", "Operator"].includes(role);
  const canCancel = ["Admin", "Supervisor"].includes(role);

  const load = useCallback(async () => {
    try {
      const [s, h] = await Promise.all([
        apiGet<FleetState>("/fleet/state"),
        apiGet<Task[]>("/fleet/tasks?limit=25"),
      ]);
      setState(s);
      setHistory(h);
      setError(null);
      loadedOnce.current = true;
    } catch (e) {
      if (!loadedOnce.current) setError(errorDetail(e));
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
  }, [load]);

  const create = useCallback(async () => {
    if (!origin || !destination) {
      setFormError("Pick both an origin and a destination.");
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      await apiPost("/fleet/tasks", { origin, destination, payload, priority });
      setPayload("");
      await load();
    } catch (e) {
      setFormError(errorDetail(e));
    } finally {
      setBusy(false);
    }
  }, [origin, destination, payload, priority, load]);

  const cancel = useCallback(async (id: number) => {
    try {
      await apiPost(`/fleet/tasks/${id}/cancel`, {});
      await load();
    } catch (e) {
      await load();
      setError(errorDetail(e));
    }
  }, [load]);

  const stations = Object.keys(state?.stations ?? {}).filter((s) => s !== "Charge Dock");
  const live = state?.tasks ?? [];
  const kpis = state?.kpis;
  const vehicleName = new Map((state?.vehicles ?? []).map((v) => [v.id, v.name]));
  // The history list already holds the live rows too; show only settled ones there.
  const settled = history.filter((t) => t.status === "Delivered" || t.status === "Cancelled");

  return (
    <section className="mt-8 space-y-6">
      <div>
        <h2 className="text-3xl font-bold">Transport Tasks</h2>
        <p className="text-slate-400 mt-2">
          Movement orders the dispatch engine works from. The plant raises its own
          (source <span className="text-slate-300">mes</span>) the moment a machine
          finishes a batch; this form raises the rest.
        </p>
      </div>

      {kpis && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Kpi title="Waiting" value={kpis.pending_tasks} />
          <Kpi title="Moving now" value={kpis.active_tasks} />
          <Kpi title="Delivered (24h)" value={kpis.delivered_24h} />
          <Kpi title="Avg wait to assign"
            value={kpis.avg_wait_s != null ? `${Math.round(kpis.avg_wait_s)}s` : "—"} />
        </div>
      )}

      {error && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 text-red-300 p-4">{error}</div>
      )}

      {canCreate && (
        <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
          <h3 className="text-lg font-semibold mb-4">Raise a movement order</h3>
          <div className="grid md:grid-cols-5 gap-3">
            <label className="flex flex-col gap-1 text-sm text-slate-400">
              Origin
              <select value={origin} onChange={(e) => setOrigin(e.target.value)}
                className="rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-slate-200">
                <option value="">Choose…</option>
                {stations.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-sm text-slate-400">
              Destination
              <select value={destination} onChange={(e) => setDestination(e.target.value)}
                className="rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-slate-200">
                <option value="">Choose…</option>
                {stations.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-sm text-slate-400 md:col-span-2">
              Payload
              <input value={payload} onChange={(e) => setPayload(e.target.value)}
                placeholder="e.g. Bin 3 — finished parts"
                className="rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-slate-200" />
            </label>
            <label className="flex flex-col gap-1 text-sm text-slate-400">
              Priority
              <select value={priority} onChange={(e) => setPriority(Number(e.target.value))}
                className="rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 text-slate-200">
                <option value={3}>High</option>
                <option value={2}>Normal</option>
                <option value={1}>Low</option>
              </select>
            </label>
          </div>
          {formError && (
            <p className="text-red-300 text-sm mt-3">{formError}</p>
          )}
          <button onClick={create} disabled={busy}
            className="mt-4 rounded-xl bg-white text-slate-950 font-semibold px-4 py-2.5 disabled:opacity-50">
            Dispatch it
          </button>
        </div>
      )}

      {/* ── Live queue ─────────────────────────────────────── */}
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 overflow-x-auto">
        <h3 className="text-lg font-semibold mb-3">Live queue</h3>
        {live.length === 0 ? (
          <p className="text-slate-400 text-sm">
            Nothing waiting or moving. Raise an order above, or let a running
            machine raise one for you.
          </p>
        ) : (
          <table className="w-full text-sm min-w-[640px]">
            <thead>
              <tr className="text-left text-slate-500 border-b border-slate-800">
                <th className="py-2 pr-4">#</th>
                <th className="py-2 pr-4">Move</th>
                <th className="py-2 pr-4">Payload</th>
                <th className="py-2 pr-4">Priority</th>
                <th className="py-2 pr-4">Source</th>
                <th className="py-2 pr-4">Status</th>
                <th className="py-2 pr-4">Vehicle</th>
                {canCancel && <th className="py-2"></th>}
              </tr>
            </thead>
            <tbody>
              {live.map((t) => (
                <tr key={t.id} className="border-b border-slate-800/60 last:border-0">
                  <td className="py-2 pr-4 text-slate-500">{t.id}</td>
                  <td className="py-2 pr-4 font-semibold">{t.origin} <span className="text-slate-500">to</span> {t.destination}</td>
                  <td className="py-2 pr-4 text-slate-400">{t.payload || "—"}</td>
                  <td className="py-2 pr-4 text-slate-300">{PRIORITY_LABEL[t.priority] ?? t.priority}</td>
                  <td className="py-2 pr-4 text-slate-400">{t.source}</td>
                  <td className="py-2 pr-4">
                    <span className={`rounded-full px-2.5 py-0.5 text-xs border ${STATUS_BADGE[t.status] ?? STATUS_BADGE.Pending}`}>
                      {t.status}
                    </span>
                  </td>
                  <td className="py-2 pr-4 text-slate-300">
                    {t.vehicle_id ? vehicleName.get(t.vehicle_id) ?? `#${t.vehicle_id}` : "—"}
                  </td>
                  {canCancel && (
                    <td className="py-2 text-right">
                      {(t.status === "Pending" || t.status === "Assigned") && (
                        <button onClick={() => cancel(t.id)}
                          className="rounded-lg border border-slate-700 px-3 py-1 text-xs text-slate-300 hover:bg-slate-800">
                          Cancel
                        </button>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* ── Recently settled ───────────────────────────────── */}
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5 overflow-x-auto">
        <h3 className="text-lg font-semibold mb-3">Recently settled</h3>
        {settled.length === 0 ? (
          <p className="text-slate-400 text-sm">No deliveries yet.</p>
        ) : (
          <table className="w-full text-sm min-w-[560px]">
            <thead>
              <tr className="text-left text-slate-500 border-b border-slate-800">
                <th className="py-2 pr-4">#</th>
                <th className="py-2 pr-4">Move</th>
                <th className="py-2 pr-4">Payload</th>
                <th className="py-2 pr-4">Status</th>
                <th className="py-2">Done at</th>
              </tr>
            </thead>
            <tbody>
              {settled.map((t) => (
                <tr key={t.id} className="border-b border-slate-800/60 last:border-0">
                  <td className="py-2 pr-4 text-slate-500">{t.id}</td>
                  <td className="py-2 pr-4">{t.origin} <span className="text-slate-500">to</span> {t.destination}</td>
                  <td className="py-2 pr-4 text-slate-400">{t.payload || "—"}</td>
                  <td className="py-2 pr-4">
                    <span className={`rounded-full px-2.5 py-0.5 text-xs border ${STATUS_BADGE[t.status] ?? STATUS_BADGE.Pending}`}>
                      {t.status}
                    </span>
                  </td>
                  <td className="py-2 text-slate-400">
                    {t.completed_at ? t.completed_at.replace("T", " ").slice(0, 19) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

function Kpi({ title, value }: { title: string; value: string | number }) {
  return (
    <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <p className="text-slate-400 text-sm">{title}</p>
      <h3 className="text-2xl font-bold mt-2">{value}</h3>
    </div>
  );
}
