"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { formatFactValue, provenanceTone, stateNotice, type Fact } from "../lib/evidence";
import { money } from "../lib/money";

// Mirrors ai/root_cause.py (ADR-0025).
type Contributor = {
  key: string; label: string; mechanism: string; units: number | null; minutes: number | null;
  money: number | null; cause_label: string; basis: string; facts: Fact[];
};
type RootCause = {
  generated_at: string; days: number; state: string; headline: string;
  gap: { state: string; planned_units: number; actual_units: number; gap_units: number | null; gap_money: number | null };
  contributors: Contributor[];
  by_machine: { machine: string; units: number; quality_units: number; performance_units: number; availability_minutes: number; logged_minutes: number }[];
  measured_loss_units: number; attributed_units: number; unattributed_units: number;
  measured_loss_money: number | null; attributed_money: number | null;
  unexplained_units: number | null; attributed_share_of_gap: number | null;
  currency: string | null; denominator_note: string; facts: Fact[];
};

// How firmly the evidence supports each line. A confirmed mechanism and a
// correlated event must not look alike, so they do not share a colour.
const CAUSE_CLASS: Record<string, string> = {
  "CAUSE CONFIRMED": "text-emerald-300 border-emerald-500/40",
  "LIKELY CONTRIBUTOR": "text-sky-300 border-sky-500/40",
  "CORRELATED EVENT": "text-amber-300 border-amber-500/40",
  "INSUFFICIENT EVIDENCE": "text-slate-400 border-slate-600",
};

const PROV_CLASS: Record<string, string> = {
  measured: "text-emerald-300 border-emerald-500/40",
  derived: "text-sky-300 border-sky-500/40",
  correlation: "text-amber-300 border-amber-500/40",
  rule: "text-violet-300 border-violet-500/40",
  model: "text-fuchsia-300 border-fuchsia-500/40",
  unknown: "text-slate-400 border-slate-600",
};

/** What one contributor cost, as this card may state it. */
export function contributorSize(c: Pick<Contributor, "units" | "minutes" | "money">): string {
  if (c.money != null) return money(c.money);
  if (c.units != null) return `${c.units.toLocaleString()} units`;
  if (c.minutes != null) return `${c.minutes.toLocaleString()} min`;
  return "not measured";
}

// The Root-Cause Explorer (ADR-0025): the gap, the losses AMP can measure, how
// much of them has a recorded reason, and the part nothing explains.
export default function RootCauseSection() {
  const [r, setR] = useState<RootCause | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setR(await apiGet<RootCause>("/root-cause"));
    } catch {
      // Stay quiet rather than break the page.
    }
  }, []);
  useEffect(() => {
    let alive = true;
    const tick = () => { if (alive) load(); };
    const first = setTimeout(tick, 0);
    return () => { alive = false; clearTimeout(first); };
  }, [load]);

  if (!r) return null;
  const notice = stateNotice(r.state);
  return (
    <section className="mt-8 rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-xl font-bold">Why are we behind?</h2>
          <p className="text-slate-300 text-sm mt-1">{r.headline}</p>
        </div>
        <span className="text-[11px] text-slate-500">last {r.days} days</span>
      </div>
      {notice && <p role="status" className="text-amber-300/90 text-xs mt-2">{r.state} · {notice}</p>}

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4">
        <div className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
          <p className="text-[11px] text-slate-500">Short of plan</p>
          <p className="text-lg font-semibold tabular-nums">
            {r.gap.gap_units == null ? "no plan set" : `${r.gap.gap_units.toLocaleString()} units`}
          </p>
        </div>
        <div className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
          <p className="text-[11px] text-slate-500">Lost capacity measured</p>
          <p className="text-lg font-semibold tabular-nums">{r.measured_loss_units.toLocaleString()} units</p>
        </div>
        <div className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
          <p className="text-[11px] text-slate-500">With a reason recorded</p>
          <p className="text-lg font-semibold tabular-nums">{r.attributed_units.toLocaleString()} units</p>
        </div>
        <div className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
          <p className="text-[11px] text-slate-500">With no reason recorded</p>
          <p className="text-lg font-semibold tabular-nums">{r.unattributed_units.toLocaleString()} units</p>
        </div>
      </div>

      <ul className="mt-4 space-y-2">
        {r.contributors.map((c) => (
          <li key={c.key} className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
            <div className="flex items-start justify-between gap-2 flex-wrap">
              <div>
                <p className="text-sm font-semibold">{c.label}</p>
                <p className="text-xs text-slate-400">{c.basis}</p>
              </div>
              <div className="text-right shrink-0">
                <p className="text-sm font-semibold tabular-nums">{contributorSize(c)}</p>
                <span className={`text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border ${CAUSE_CLASS[c.cause_label] ?? CAUSE_CLASS["INSUFFICIENT EVIDENCE"]}`}>
                  {c.cause_label}
                </span>
              </div>
            </div>
            {c.facts.length > 0 && (
              <button
                type="button"
                aria-expanded={open === c.key}
                onClick={() => setOpen(open === c.key ? null : c.key)}
                className="mt-2 text-xs text-slate-400 hover:text-white"
              >
                {open === c.key ? "Hide" : "Show"} evidence ({c.facts.length})
              </button>
            )}
            {open === c.key && (
              <ul className="mt-2 space-y-1">
                {c.facts.map((f) => (
                  <li key={f.key} className="text-xs flex flex-wrap items-baseline gap-x-2">
                    <span className="text-slate-400">{f.label}</span>
                    <span className="text-white font-semibold tabular-nums">{formatFactValue(f)}</span>
                    <span className={`text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border ${PROV_CLASS[provenanceTone(f.provenance)]}`}>
                      {f.provenance}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>

      {r.unexplained_units != null && r.unexplained_units > 0 && (
        <p className="text-xs text-slate-400 mt-3">
          <span className="text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border border-slate-600 text-slate-400 mr-2">
            UNKNOWN
          </span>
          {r.unexplained_units.toLocaleString()} units of the gap are not accounted for by the losses AMP can measure.
        </p>
      )}
      {r.by_machine.length > 0 && (
        <p className="text-xs text-slate-500 mt-2">
          Worst machines: {r.by_machine.slice(0, 3).map((m) => `${m.machine} (${m.units.toLocaleString()} units)`).join(", ")}
        </p>
      )}
      <p className="text-[11px] text-slate-500 mt-2">{r.denominator_note}</p>
    </section>
  );
}
