"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

type ChaseRow = {
  work_order_no: string;
  part_number: string;
  material_state: string;
  status: string;
  age_days: number;
  late: boolean;
  planned_end: string | null;
  target_quantity: number;
  actual_quantity: number;
  progress: number | null;
};

// Mirrors ai.flow.build_wip_aging (GET /wip-aging).
type WipAging = {
  open: number;
  late: number;
  undated: number;
  stale: number;
  stale_days: number;
  oldest_days: number | null;
  aging: { bucket: string; count: number }[];
  by_state: { state: string; count: number; avg_age_days: number; oldest_days: number }[];
  chase: ChaseRow[];
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

const STATE_LABEL: Record<string, string> = { RAW: "Raw (pre-SMT)", SEMI: "Semi (on IC)", FIN: "Finished" };

// The Work Orders view's backlog-health card: how long the open work has been
// sitting, which of it has blown its planned end, and where it stalls per
// material state. Honest by design — age from created_at, lateness only against
// a real planned_end (undated WOs reported, never assumed on time), and no
// cycle-time claims (the schema has no completion timestamp). Self-contained;
// hides on error so the CRUD below always renders.
export default function WipAgingCard() {
  const [d, setD] = useState<WipAging | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setD(await apiGet<WipAging>("/wip-aging"));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load WIP aging");
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
        Loading WIP aging…
      </div>
    );
  }
  if (error || !d) return null;

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
        <h3 className="text-lg font-semibold">Backlog health — open work orders</h3>
        <div className={`rounded-xl border px-4 py-2 text-sm ${toneClasses(d.tone)}`}>{d.verdict}</div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Open" value={d.open} />
        <Stat label="Past planned end" value={d.late} bad={d.late > 0} />
        <Stat label={`Stale (>${d.stale_days}d)`} value={d.stale} warn={d.stale > 0} />
        <Stat label="Oldest open" value={d.oldest_days === null ? "—" : `${d.oldest_days}d`} />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {/* Where work stalls */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Where work is sitting</h4>
          {d.by_state.length === 0 ? (
            <p className="text-slate-500 text-sm">Nothing open.</p>
          ) : (
            <ul className="space-y-2">
              {d.by_state.map((s) => (
                <li key={s.state} className="flex items-center justify-between gap-3 text-sm">
                  <span className="font-semibold">{STATE_LABEL[s.state] || s.state}</span>
                  <span className="text-xs text-slate-400 whitespace-nowrap">
                    {s.count} open · avg {s.avg_age_days}d · oldest {s.oldest_days}d
                  </span>
                </li>
              ))}
            </ul>
          )}
          {d.aging.length > 0 && (
            <p className="text-xs text-slate-500 mt-3">
              Age profile: {d.aging.map((a) => `${a.bucket}: ${a.count}`).join(" · ")}
              {d.undated > 0 && ` · ${d.undated} with no planned end`}
            </p>
          )}
        </div>

        {/* Oldest first */}
        <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
          <h4 className="text-sm font-semibold text-slate-300 mb-3">Chase first (oldest open)</h4>
          {d.chase.length === 0 ? (
            <p className="text-slate-500 text-sm">Backlog clear.</p>
          ) : (
            <ul className="space-y-2">
              {d.chase.slice(0, 6).map((c) => (
                <li key={c.work_order_no} className="flex items-center justify-between gap-3 text-sm">
                  <span className="min-w-0 truncate">
                    <span className="font-semibold">{c.work_order_no}</span>
                    <span className="text-slate-400"> · {c.part_number} · {c.material_state}</span>
                  </span>
                  <span className={`whitespace-nowrap text-xs ${c.late ? "text-red-400" : "text-slate-400"}`}>
                    {c.age_days}d open{c.late ? " · LATE" : ""}
                    {c.progress !== null ? ` · ${c.progress}%` : ""}
                  </span>
                </li>
              ))}
            </ul>
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
