"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import UnitRateEditor from "./UnitRateEditor";

// The money story: the OEE gap and downtime, both valued off the one per-tenant
// £/good-unit rate. Reads the recovery read-model (upside) and the management
// summary (downtime loss); shows £ when a rate is set, honest units otherwise.
type Recovery = {
  has_data: boolean;
  oee: number;
  world_class: number;
  gap_points: number;
  unit_value_gbp: number | null;
  recoverable_units_per_year: number;
  recoverable_value_per_year: number | null;
};
type Management = {
  total_downtime_minutes: number;
  estimated_loss_units: number;
  estimated_loss_value: number;
  unit_value_gbp: number | null;
};

const gbp = (n: number) => `£${Math.round(n).toLocaleString()}`;
const units = (n: number) => Math.round(n).toLocaleString();

export default function MoneyStorySnapshot({ isAdmin = false }: { isAdmin?: boolean }) {
  const [rec, setRec] = useState<Recovery | null>(null);
  const [mgmt, setMgmt] = useState<Management | null>(null);

  const load = useCallback(async () => {
    const [r, m] = await Promise.allSettled([
      apiGet<Recovery>("/recovery-summary"),
      apiGet<Management>("/analytics/management"),
    ]);
    if (r.status === "fulfilled") setRec(r.value);
    if (m.status === "fulfilled") setMgmt(m.value);
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  // Nothing to value until there is production to measure.
  if (!rec || !rec.has_data) return null;

  const rate = rec.unit_value_gbp;
  const priced = rate != null;

  return (
    <section className="mt-8 space-y-4">
      <div>
        <h2 className="text-3xl font-bold">OEE in money</h2>
        <p className="text-slate-400 mt-2">
          The OEE gap and downtime, valued off {priced ? (
            <span className="text-slate-200">£{rate.toLocaleString()} / good unit</span>
          ) : (
            <span className="text-slate-300">your unit value</span>
          )}
          {priced ? "" : isAdmin ? " — set your rate below to see £" : " — ask an Admin to set your rate to see £"}.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="rounded-2xl border border-red-500/30 bg-red-500/5 p-6">
          <p className="text-xs font-semibold uppercase tracking-wide text-red-300/80">Downtime loss · recent window</p>
          <p className="mt-2 text-4xl font-bold text-red-300 tabular-nums">
            {priced ? gbp(mgmt?.estimated_loss_value ?? 0) : `${units(mgmt?.estimated_loss_units ?? 0)} units`}
          </p>
          <p className="mt-2 text-sm text-slate-400">
            {units(mgmt?.total_downtime_minutes ?? 0)} min of downtime ≈ {units(mgmt?.estimated_loss_units ?? 0)} good units not made
          </p>
        </div>

        <div className="rounded-2xl border border-emerald-500/30 bg-emerald-500/5 p-6">
          <p className="text-xs font-semibold uppercase tracking-wide text-emerald-300/80">Recovery upside · per year</p>
          <p className="mt-2 text-4xl font-bold text-emerald-300 tabular-nums">
            {rec.gap_points === 0
              ? (priced ? "£0" : "0 units")
              : priced
                ? gbp(rec.recoverable_value_per_year ?? 0)
                : `+${units(rec.recoverable_units_per_year)} units`}
          </p>
          <p className="mt-2 text-sm text-slate-400">
            {rec.gap_points === 0
              ? `at or above the ${rec.world_class}% world-class benchmark`
              : `closing OEE ${rec.oee}% → ${rec.world_class}% ≈ ${units(rec.recoverable_units_per_year)} more good units / yr`}
          </p>
        </div>
      </div>

      <div className="flex items-center justify-between text-[11px] text-slate-500 gap-3 flex-wrap">
        <span>
          Both figures are units × the same £/good-unit — one rate drives the whole dashboard, and stays units-only until you set it.
        </span>
        <span className="flex items-center gap-2 whitespace-nowrap">
          <span>
            Unit value:{" "}
            {priced ? (
              <span className="text-slate-300 tabular-nums">£{rate.toLocaleString()}</span>
            ) : (
              <span className="text-slate-500">not set</span>
            )}
          </span>
          <UnitRateEditor rate={rate} isAdmin={isAdmin} onSaved={load} />
        </span>
      </div>
    </section>
  );
}
