"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors the backend pulse (ai/pulse.py build_pulse).
type Pulse = {
  fleet: {
    machines: number;
    avg_health: number;
    needs_attention: number;
    worst: { machine_id: number; name: string; health_score: number; health_band: string } | null;
  };
  agents: { agents_active: number; actions_7d: number; auto_rate: number; awaiting_you: number };
  headline: string;
};

function healthColor(score: number) {
  if (score >= 80) return "text-emerald-400";
  if (score >= 55) return "text-yellow-400";
  if (score >= 35) return "text-orange-400";
  return "text-red-400";
}

// The owner's one-glance command header — fleet health + agent workload.
// Self-contained: fetches its own pulse and refreshes, so it can sit on any
// screen (Mission Control, Overview, …) without prop-drilling.
export default function FactoryPulse() {
  const [pulse, setPulse] = useState<Pulse | null>(null);

  const load = useCallback(async () => {
    try {
      setPulse(await apiGet<Pulse>("/mission-control/pulse"));
    } catch {
      // A glanceable header — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!pulse) return null;

  return (
    <div className="rounded-2xl border border-indigo-500/30 bg-gradient-to-br from-indigo-500/10 to-slate-900 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-indigo-300">Factory pulse</h3>
          <p className="text-lg font-semibold mt-1">{pulse.headline}</p>
        </div>
        {pulse.fleet.worst && pulse.fleet.needs_attention > 0 && (
          <span className="rounded-full px-3 py-1 text-xs border border-red-500/40 bg-red-500/10 text-red-300">
            worst: {pulse.fleet.worst.name} ({pulse.fleet.worst.health_score})
          </span>
        )}
      </div>
      <div className="mt-5 grid grid-cols-2 md:grid-cols-4 gap-4">
        <PulseTile label="Fleet health" value={pulse.fleet.avg_health} color={healthColor(pulse.fleet.avg_health)} />
        <PulseTile label="Need attention" value={pulse.fleet.needs_attention} />
        <PulseTile label="Awaiting you" value={pulse.agents.awaiting_you} highlight={pulse.agents.awaiting_you > 0} />
        <PulseTile label="Autonomy" value={`${pulse.agents.auto_rate}%`} sub={`${pulse.agents.actions_7d} actions / 7d`} />
      </div>
    </div>
  );
}

function PulseTile({
  label,
  value,
  sub,
  color,
  highlight,
}: {
  label: string;
  value: string | number;
  sub?: string;
  color?: string;
  highlight?: boolean;
}) {
  return (
    <div className={`rounded-xl border p-4 ${highlight ? "border-amber-500/40 bg-amber-500/10" : "border-slate-800 bg-slate-900/60"}`}>
      <p className="text-slate-400 text-xs">{label}</p>
      <p className={`text-2xl font-bold mt-1 ${highlight ? "text-amber-300" : color ?? ""}`}>{value}</p>
      {sub && <p className="text-[11px] text-slate-500 mt-0.5">{sub}</p>}
    </div>
  );
}
