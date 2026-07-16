"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import MachineDetailDrawer from "./MachineDetailDrawer";

// Mirrors the backend OEE summary (ai/oee.py build_oee_summary).
type MachineOee = {
  machine_id: number;
  name: string;
  oee: number;
  availability: number;
  performance: number;
  quality: number;
  has_data: boolean;
};

type OeeSummary = {
  days: number;
  world_class: number;
  plant: { oee: number; availability: number; performance: number; quality: number; has_data: boolean };
  machine_count: number;
  machines_with_data: number;
  biggest_drag: "availability" | "performance" | "quality" | null;
  daily: { date: string; oee: number }[];
  worst: MachineOee | null;
  best: MachineOee | null;
  machines: MachineOee[];
};

function oeeColor(v: number) {
  if (v >= 85) return "text-emerald-400"; // world-class
  if (v >= 60) return "text-yellow-400";
  if (v >= 40) return "text-orange-400";
  return "text-red-400";
}

function barColor(v: number) {
  if (v >= 85) return "bg-emerald-500";
  if (v >= 60) return "bg-yellow-500";
  if (v >= 40) return "bg-orange-500";
  return "bg-red-500";
}

function wk(iso: string) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { weekday: "short" });
}

function Component({ label, value, drag }: { label: string; value: number; drag: boolean }) {
  return (
    <div>
      <div className="flex items-center justify-between text-xs mb-1">
        <span className={drag ? "text-amber-300 font-semibold" : "text-slate-400"}>
          {label}
          {drag ? " · biggest drag" : ""}
        </span>
        <span className="text-slate-300 tabular-nums">{value}%</span>
      </div>
      <div className="h-2 rounded-full bg-slate-800 overflow-hidden">
        <div className={`h-full ${barColor(value)}`} style={{ width: `${Math.min(100, value)}%` }} />
      </div>
    </div>
  );
}

// The plant's headline OEE, glanceable on the Overview home: the number plant
// managers live by, broken into Availability / Performance / Quality with the
// biggest drag flagged, plus the lowest and highest machine. Self-contained —
// fetches its own summary and refreshes, so it drops onto any screen without
// prop-drilling. Renders nothing until there's production to measure.
export default function OeeSnapshot() {
  const [s, setS] = useState<OeeSummary | null>(null);
  const [machine, setMachine] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      setS(await apiGet<OeeSummary>("/oee-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (!s || !s.plant.has_data) return null;

  const { plant, worst, best } = s;

  return (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">OEE · last 7 days</h3>
          <p className="text-slate-400 text-sm mt-1">
            Availability × Performance × Quality · {s.machines_with_data} of {s.machine_count} machine
            {s.machine_count !== 1 ? "s" : ""} reporting
          </p>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${oeeColor(plant.oee)}`}>{plant.oee}%</p>
          <p className="text-[11px] text-slate-500">plant OEE · world-class {s.world_class}%</p>
        </div>
      </div>
      <div className="mt-5 grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="space-y-3">
          <Component label="Availability" value={plant.availability} drag={s.biggest_drag === "availability"} />
          <Component label="Performance" value={plant.performance} drag={s.biggest_drag === "performance"} />
          <Component label="Quality" value={plant.quality} drag={s.biggest_drag === "quality"} />
        </div>
        <div>
          <p className="text-xs text-slate-500 mb-2">Machines</p>
          <div className="space-y-2">
            {worst && (
              <button
                type="button"
                onClick={() => setMachine(worst.machine_id)}
                className="w-full flex items-center justify-between rounded-lg border border-slate-700 px-3 py-1.5 text-sm hover:border-slate-500 hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
                title={`${worst.name} — open machine cockpit`}
              >
                <span className="text-slate-400">Lowest · {worst.name}</span>
                <span className={`font-semibold ${oeeColor(worst.oee)}`}>{worst.oee}%</span>
              </button>
            )}
            {best && best.machine_id !== worst?.machine_id && (
              <button
                type="button"
                onClick={() => setMachine(best.machine_id)}
                className="w-full flex items-center justify-between rounded-lg border border-slate-700 px-3 py-1.5 text-sm hover:border-slate-500 hover:bg-slate-800 transition focus:outline-none focus:ring-2 focus:ring-slate-600"
                title={`${best.name} — open machine cockpit`}
              >
                <span className="text-slate-400">Highest · {best.name}</span>
                <span className={`font-semibold ${oeeColor(best.oee)}`}>{best.oee}%</span>
              </button>
            )}
          </div>
        </div>
      </div>
      {s.daily.some((d) => d.oee > 0) && (
        <div className="mt-6">
          <p className="text-xs text-slate-500 mb-2">7-day OEE trend</p>
          <div className="flex items-end gap-2 h-16">
            {s.daily.map((d) => {
              const h = Math.max(4, Math.round((d.oee / 100) * 56));
              return (
                <div
                  key={d.date}
                  className="flex-1 flex flex-col items-center justify-end gap-1"
                  title={`${d.oee}% on ${d.date}`}
                >
                  <span className="text-[10px] text-slate-400">{d.oee || ""}</span>
                  <div className={`w-full rounded-t ${barColor(d.oee)}`} style={{ height: `${h}px` }} />
                  <span className="text-[10px] text-slate-500">{wk(d.date)}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}
      {machine != null && (
        <MachineDetailDrawer machineId={machine} onClose={() => setMachine(null)} onChanged={load} />
      )}
    </div>
  );
}
