"use client";
import React, { useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";

interface Protocol { key: string; name: string; port: number; library: string; transport: string; desc: string; }
interface Device {
  id: number; device_code: string; device_name: string; device_type: string;
  protocol: string; ip_address: string | null; status: string; linked_machine_id: number | null;
}
interface Signal {
  id: number; device_id: number; signal_name: string; signal_value: string;
  numeric_value: number; unit: string | null; quality: string; source_protocol: string; created_at: string;
}

export default function IndustrialConnectivity() {
  const [protocols, setProtocols] = useState<Protocol[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [form, setForm] = useState({ device_code: "", device_name: "", protocol: "", ip_address: "" });
  const [msg, setMsg] = useState("");

  const load = () => {
    apiGet<Protocol[]>("/industrial/protocols").then(setProtocols).catch(() => {});
    apiGet<Device[]>("/industrial/devices").then(setDevices).catch(() => {});
    apiGet<Signal[]>("/industrial/signals").then(setSignals).catch(() => {});
  };
  useEffect(() => {
    load();
    const t = setInterval(() => apiGet<Signal[]>("/industrial/signals").then(setSignals).catch(() => {}), 8000);
    return () => clearInterval(t);
  }, []);

  // latest value per (device, signal_name) — signals come newest-first
  function latestFor(deviceId: number) {
    const seen: Record<string, Signal> = {};
    for (const s of signals) {
      if (s.device_id !== deviceId) continue;
      if (!seen[s.signal_name]) seen[s.signal_name] = s;
    }
    return Object.values(seen);
  }

  async function addDevice(e: React.FormEvent) {
    e.preventDefault(); setMsg("");
    try {
      await apiPost("/industrial/devices", {
        device_code: form.device_code,
        device_name: form.device_name,
        device_type: "PLC",
        protocol: form.protocol || (protocols[0]?.name ?? "Modbus TCP"),
        ip_address: form.ip_address,
        status: "Online",
      });
      setForm({ device_code: "", device_name: "", protocol: "", ip_address: "" });
      setMsg("✓ Device added — signals will start flowing.");
      load();
    } catch (e: any) {
      setMsg(e?.message?.replace(/^POST .* failed: \d+ /, "") || "Failed to add device — Admin/Supervisor only.");
    }
  }

  const statusBadge = (s: string) =>
    s === "Online"
      ? "text-green-400 border-green-500/40 bg-green-500/10"
      : "text-red-400 border-red-500/40 bg-red-500/10";

  return (
    <section className="mt-8 space-y-6">
      <div>
        <h2 className="text-3xl font-bold">Industrial Connectivity</h2>
        <p className="text-slate-400 mt-2 text-sm">
          Connect shop-floor PLCs over their native protocols through FlowMES's adapter layer. Live signals update every few seconds.
        </p>
      </div>

      {/* Supported protocols */}
      <div>
        <h3 className="text-lg font-semibold mb-3">Supported protocols</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {protocols.map((p) => (
            <div key={p.key} className="rounded-2xl bg-slate-900 border border-slate-800 p-4">
              <div className="flex items-center justify-between">
                <span className="font-semibold">{p.name}</span>
                <span className="text-xs text-slate-500 font-mono">:{p.port}</span>
              </div>
              <p className="text-slate-400 text-xs mt-1.5 leading-relaxed">{p.desc}</p>
              <div className="flex items-center gap-2 mt-3 text-[11px]">
                <span className="text-slate-400 bg-slate-800 border border-slate-700 rounded px-2 py-0.5">{p.transport}</span>
                <span className="text-indigo-300 bg-indigo-500/10 border border-indigo-500/30 rounded px-2 py-0.5 font-mono">{p.library}</span>
              </div>
            </div>
          ))}
        </div>
        <p className="text-slate-500 text-xs mt-2">
          Real drivers run on an on-site edge agent using the library shown; the cloud demo streams simulated signals through the same adapters.
        </p>
      </div>

      {/* Add device */}
      <form onSubmit={addDevice} className="rounded-2xl bg-slate-900 border border-slate-800 p-5 grid grid-cols-1 md:grid-cols-5 gap-3">
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Device code" value={form.device_code} onChange={(e) => setForm({ ...form, device_code: e.target.value })} required />
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="Device name" value={form.device_name} onChange={(e) => setForm({ ...form, device_name: e.target.value })} required />
        <select className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" value={form.protocol} onChange={(e) => setForm({ ...form, protocol: e.target.value })}>
          <option value="">Protocol…</option>
          {protocols.map((p) => <option key={p.key} value={p.name}>{p.name}</option>)}
        </select>
        <input className="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-sm" placeholder="IP : port" value={form.ip_address} onChange={(e) => setForm({ ...form, ip_address: e.target.value })} />
        <button type="submit" className="rounded-xl bg-white text-slate-950 font-semibold px-4 py-2 text-sm">Connect device</button>
        {msg && <p className={`md:col-span-5 text-sm ${msg.startsWith("✓") ? "text-green-400" : "text-red-400"}`}>{msg}</p>}
      </form>

      {/* Connected devices + live signals */}
      <div className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
        <h3 className="text-lg font-semibold mb-4">Connected devices <span className="text-slate-500 text-sm font-normal">({devices.length})</span></h3>
        <div className="space-y-3">
          {devices.length === 0 && <p className="text-slate-400 text-sm">No devices yet.</p>}
          {devices.map((d) => {
            const latest = latestFor(d.id);
            return (
              <div key={d.id} className="rounded-xl border border-slate-800 p-4">
                <div className="flex items-center gap-3 flex-wrap mb-2">
                  <span className="font-mono font-semibold text-indigo-300">{d.device_code}</span>
                  <span className="font-medium">{d.device_name}</span>
                  <span className="text-xs text-slate-400 bg-slate-800 border border-slate-700 rounded px-2 py-0.5">{d.protocol}</span>
                  <span className="text-xs text-slate-500 font-mono">{d.ip_address || "—"}</span>
                  <span className={`ml-auto rounded-full px-2 py-0.5 text-xs border ${statusBadge(d.status)}`}>{d.status}</span>
                </div>
                <div className="flex flex-wrap gap-2">
                  {latest.length === 0 && <span className="text-slate-500 text-xs">Awaiting first poll…</span>}
                  {latest.map((s) => (
                    <span key={s.signal_name} className="text-xs bg-slate-950 border border-slate-700 rounded-lg px-2.5 py-1">
                      <span className="text-slate-400">{s.signal_name}</span>{" "}
                      <span className="font-mono font-semibold text-green-400">{s.numeric_value}</span>{" "}
                      <span className="text-slate-500">{s.unit}</span>
                    </span>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
