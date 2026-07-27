"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// Mirrors ai.stock_health.build_stock_health (GET /stock-health).
type StockHealth = {
  window_days: number;
  overstock_days: number;
  total_items: number;
  dead: number;
  overstocked: number;
  healthy: number;
  empty: number;
  dead_units: number;
  excess_units: number;
  flagged_items: number;
  flagged_units: number;
  items: {
    item_code: string;
    item_name: string;
    current_stock: number;
    unit: string;
    daily_burn: number;
    days_of_cover: number | null;
    state: string;
    flagged_units: number;
  }[];
  verdict: string;
  tone: string;
};

// Mirrors ai.stock_accuracy.build_stock_accuracy (GET /stock-accuracy).
type StockAccuracy = {
  window_days: number;
  target: number;
  total_items: number;
  counted_items: number;
  count_coverage: number;
  accurate_items: number;
  inaccurate_items: number;
  over_items: number;
  under_items: number;
  accuracy_rate: number | null;
  net_variance_units: number;
  abs_variance_units: number;
  items: {
    item_code: string;
    item_name: string;
    book_qty: number;
    physical_qty: number;
    variance: number;
    direction: string;
    unit: string;
  }[];
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

// The stock-integrity pair for the Inventory view: what the holding is worth
// keeping (dead stock / overstock) and whether the records can be trusted
// (cycle-count accuracy). Each card fetches independently and hides on error,
// so the CRUD sections below always render.
export default function StockIntegrityCards() {
  const [health, setHealth] = useState<StockHealth | null>(null);
  const [acc, setAcc] = useState<StockAccuracy | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const [h, a] = await Promise.allSettled([
      apiGet<StockHealth>("/stock-health"),
      apiGet<StockAccuracy>("/stock-accuracy"),
    ]);
    setHealth(h.status === "fulfilled" ? h.value : null);
    setAcc(a.status === "fulfilled" ? a.value : null);
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !health && !acc) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5 text-slate-400 text-sm">
        Loading stock integrity…
      </div>
    );
  }
  if (!health && !acc) return null;

  return (
    <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
      {health && (
        <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
          <div className="flex flex-col gap-2">
            <h3 className="text-lg font-semibold">Stock health — dead &amp; overstock</h3>
            <div className={`rounded-xl border px-4 py-2 text-sm ${toneClasses(health.tone)}`}>{health.verdict}</div>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Stat label="Dead items" value={health.dead} bad={health.dead > 0} />
            <Stat label="Idle units" value={health.dead_units.toLocaleString()} bad={health.dead_units > 0} />
            <Stat label="Overstocked" value={health.overstocked} warn={health.overstocked > 0} />
            <Stat label="Excess units" value={health.excess_units.toLocaleString()} warn={health.excess_units > 0} />
          </div>
          {health.items.length > 0 && (
            <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
              <h4 className="text-sm font-semibold text-slate-300 mb-3">Review first</h4>
              <ul className="space-y-2">
                {health.items.slice(0, 5).map((r) => (
                  <li key={r.item_code} className="flex items-center justify-between gap-3 text-sm">
                    <span className="min-w-0 truncate">
                      <span className="font-semibold">{r.item_name}</span>
                      <span className="text-slate-500"> · {r.item_code}</span>
                    </span>
                    <span className={`whitespace-nowrap text-xs ${r.state === "dead" ? "text-red-400" : "text-amber-400"}`}>
                      {r.state === "dead"
                        ? `dead · ${r.flagged_units.toLocaleString()} ${r.unit || "units"} idle`
                        : `overstock · ${r.flagged_units.toLocaleString()} ${r.unit || "units"} excess`}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}

      {acc && (
        <section className="rounded-2xl border border-slate-800 bg-slate-900 p-5 space-y-4">
          <div className="flex flex-col gap-2">
            <h3 className="text-lg font-semibold">Record accuracy — cycle counts</h3>
            <div className={`rounded-xl border px-4 py-2 text-sm ${toneClasses(acc.tone)}`}>{acc.verdict}</div>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Stat
              label={`IRA (target ${acc.target}%)`}
              value={acc.accuracy_rate === null ? "—" : `${acc.accuracy_rate}%`}
              bad={acc.accuracy_rate !== null && acc.accuracy_rate < acc.target}
            />
            <Stat label="Master counted" value={`${acc.count_coverage}%`} warn={acc.count_coverage < 100 && acc.counted_items > 0} />
            <Stat label="Records off" value={acc.inaccurate_items} bad={acc.inaccurate_items > 0} />
            <Stat label="Units off (abs)" value={acc.abs_variance_units.toLocaleString()} bad={acc.abs_variance_units > 0} />
          </div>
          {acc.items.length > 0 && (
            <div className="rounded-xl bg-slate-950 border border-slate-800 p-4">
              <h4 className="text-sm font-semibold text-slate-300 mb-3">Furthest off the book</h4>
              <ul className="space-y-2">
                {acc.items.slice(0, 5).map((r) => (
                  <li key={r.item_code} className="flex items-center justify-between gap-3 text-sm">
                    <span className="min-w-0 truncate">
                      <span className="font-semibold">{r.item_name}</span>
                      <span className="text-slate-500"> · {r.item_code}</span>
                    </span>
                    <span className={`whitespace-nowrap text-xs ${r.direction === "under" ? "text-red-400" : "text-amber-400"}`}>
                      book {r.book_qty} → shelf {r.physical_qty} ({r.variance > 0 ? "+" : ""}{r.variance})
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}
    </div>
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
