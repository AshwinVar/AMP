"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import PartRunwayDrawer from "./PartRunwayDrawer";

// Mirrors the backend days-of-cover read-model (ai/coverage.py build_coverage_summary).
type CoverageItem = {
  item_code: string;
  item_name: string;
  current_stock: number;
  unit: string;
  supplier: string | null;
  daily_burn: number;
  days_of_cover: number | null;
  stockout_date: string | null;
  state: "out" | "critical" | "watch";
};

type CoverageSummary = {
  window_days: number;
  critical_days: number;
  total_items: number;
  out_of_stock: number;
  critical: number;
  watch: number;
  running_out: number;
  items: CoverageItem[];
};

// Each runway state gets a consistent accent (out=red, critical=orange, watch=yellow).
const stateChip: Record<CoverageItem["state"], string> = {
  out: "border-red-500/40 bg-red-500/10 text-red-300",
  critical: "border-orange-500/40 bg-orange-500/10 text-orange-300",
  watch: "border-yellow-500/40 bg-yellow-500/10 text-yellow-300",
};
const barColor: Record<CoverageItem["state"], string> = {
  out: "bg-red-500",
  critical: "bg-orange-500",
  watch: "bg-yellow-500",
};

function coverLabel(i: CoverageItem) {
  if (i.state === "out") return "out of stock";
  const d = i.days_of_cover ?? 0;
  return `${d % 1 === 0 ? d : d.toFixed(1)} day${d === 1 ? "" : "s"} left`;
}

function fmtDate(iso: string | null) {
  if (!iso) return null;
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// A glanceable stockout forecast — the items burning down fastest and when they
// run dry, at the rate we're actually consuming them (not just below a static
// reorder line). Self-contained: fetches its own summary and refreshes, so it
// drops onto any screen without prop-drilling. Renders nothing when nothing is
// on track to run out. Each row opens the part drill-down: why it's running out
// and whether what's on order lands in time.
export default function CoverageSnapshot() {
  const [cov, setCov] = useState<CoverageSummary | null>(null);
  const [part, setPart] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setCov(await apiGet<CoverageSummary>("/coverage-summary"));
    } catch {
      // A glanceable card — stay quiet on error rather than break the page.
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  // Scale each runway bar against the widest window we surface (watch horizon),
  // so shorter runways read as visibly emptier. "out" always shows empty.
  const horizon = cov ? cov.critical_days * 3 : 0;

  // Mount the drawer OUTSIDE the card's data guard: `items` only holds at-risk
  // rows, so a 30s poll that clears them would otherwise unmount a drawer the user
  // is mid-read on. Its selected part lives in this component, so it must outlive
  // the card.
  return (
    <>
      {part && <PartRunwayDrawer itemCode={part} onClose={() => setPart(null)} />}
      {cov && cov.items.length > 0 && (
    <div className="rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
      <div className="flex items-start justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-300">Runway · days of cover</h3>
          <p className="text-slate-400 text-sm mt-1">
            at the last {cov.window_days} days&apos; burn rate
            {cov.watch > 0 && ` · ${cov.watch} more to watch`}
          </p>
        </div>
        <div className="text-right">
          <p className={`text-3xl font-bold ${cov.out_of_stock > 0 ? "text-red-400" : "text-orange-400"}`}>
            {cov.running_out}
          </p>
          <p className="text-[11px] text-slate-500">
            run{cov.running_out === 1 ? "s" : ""} dry within {cov.critical_days}d
            {cov.out_of_stock > 0 && ` · ${cov.out_of_stock} out`}
          </p>
        </div>
      </div>
      <div className="mt-5 space-y-2">
        {cov.items.map((i) => {
          const pct = i.state === "out"
            ? 0
            : Math.max(4, Math.min(100, Math.round(((i.days_of_cover ?? 0) / horizon) * 100)));
          const dateStr = fmtDate(i.stockout_date);
          return (
            <button
              key={i.item_code}
              type="button"
              onClick={() => setPart(i.item_code)}
              className="flex w-full items-center gap-3 rounded-lg px-1 py-0.5 text-left hover:bg-slate-800/50 transition"
              title={`${i.item_name} (${i.item_code}) · ${i.current_stock} ${i.unit} · burning ${i.daily_burn}/day${i.supplier ? ` · ${i.supplier}` : ""} — open part detail`}
            >
              <span className="w-28 shrink-0 text-sm text-slate-300 truncate">{i.item_name}</span>
              <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${stateChip[i.state]}`}>
                {i.state}
              </span>
              <div className="flex-1 h-2 rounded bg-slate-800 overflow-hidden">
                <div className={`h-full ${barColor[i.state]}`} style={{ width: `${pct}%` }} />
              </div>
              <span className="w-24 shrink-0 text-right text-xs text-slate-400">
                {coverLabel(i)}
                {dateStr && i.state !== "out" && <span className="text-slate-600"> · {dateStr}</span>}
              </span>
            </button>
          );
        })}
      </div>
    </div>
      )}
    </>
  );
}
