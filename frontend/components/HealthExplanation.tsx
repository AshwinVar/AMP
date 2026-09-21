"use client";

import { useState } from "react";
import { stateNotice } from "../lib/evidence";

// Mirrors ai/machine_health.py (ADR-0027).
export type HealthRule = {
  key: string;
  label: string;
  points: number;
  max_points: number;
  reading: string;
  measured: number | string | null;
  unit: string;
  threshold: string;
  reason: string;
};

export type HealthExplanation = {
  health_score: number | null;
  band: string | null;
  band_rule: string;
  start: number;
  deductions: HealthRule[];
  clear: HealthRule[];
  checks_run: number;
  points_deducted: number;
  points_before_cap: number;
  capped: boolean;
  state: string;
  note: string;
  /** false when nothing was recorded in the risk window (state PARTIAL DATA). */
  has_recorded_input?: boolean;
  /** How many of the rules read nothing recorded. */
  rules_unmeasured?: number;
};

/**
 * The health score, taken apart (ADR-0027).
 *
 * The old card listed the reasons a score was low and stopped there, which asks
 * the reader to trust the number. This shows the arithmetic instead: what the
 * score started at, every rule that took points off — with the value it read and
 * the threshold it read against — and, behind a toggle, every rule that passed.
 * A maintenance lead can disagree with a threshold here, which is the point.
 *
 * Nothing on this card is a prediction, and it says so.
 */
export default function HealthExplanation({ x }: { x?: HealthExplanation | null }) {
  const [showClear, setShowClear] = useState(false);
  if (!x) return null;

  const notice = stateNotice(x.state);
  const scored = x.health_score != null;
  // PARTIAL DATA: the scorer had a row but nothing recorded to read for the
  // history rules. The arithmetic is shown as it is; "all rules passed" is
  // not, because six of them read nothing — the backend's note says which.
  const unrecorded = scored && x.state === "PARTIAL DATA";
  return (
    <div>
      <h3 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
        Health score, explained
      </h3>

      {scored ? (
        <p className="text-xs text-slate-400 mt-2 tabular-nums">
          {x.start} − {x.points_deducted} rule points ={" "}
          <span className="text-white font-semibold">{x.health_score}</span>/100
          {x.band && <span className="text-slate-400"> · {x.band}</span>}
        </p>
      ) : (
        // NOT MEASURED. The twin shows 100 for a machine the scorer never saw;
        // saying "Healthy" here would turn an absence into a clean bill.
        <p role="status" className="text-amber-300/90 text-xs mt-2">
          {x.state}
          {notice ? ` · ${notice}` : ""}
        </p>
      )}

      {x.capped && (
        <p className="text-[11px] text-amber-300/90 mt-1">
          The rules came to {x.points_before_cap} points; the score stops at 100, so this machine is
          further past the limit than the number can show.
        </p>
      )}

      {unrecorded && (
        <p role="status" className="text-amber-300/90 text-xs mt-2">
          {x.state} · {x.note}
        </p>
      )}

      {x.deductions.length === 0 && scored && !unrecorded && (
        <p className="text-slate-400 text-sm mt-2">
          All {x.checks_run} health rules passed — nothing was taken off.
        </p>
      )}

      {x.deductions.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {x.deductions.map((d) => (
            <li key={d.key} className="flex items-baseline justify-between gap-3">
              <span className="text-sm text-slate-300">
                {d.label}
                {/* The measurement and the threshold, always together: a rule
                    with no reading beside it is an opinion with a number on it. */}
                <span className="block text-[11px] text-slate-500">
                  read {d.reading} · rule: {d.threshold}
                </span>
              </span>
              <span className="text-sm font-semibold text-orange-400 tabular-nums shrink-0">
                −{d.points}
              </span>
            </li>
          ))}
        </ul>
      )}

      {x.clear.length > 0 && (
        <>
          <button
            type="button"
            aria-expanded={showClear}
            onClick={() => setShowClear(!showClear)}
            className="text-xs text-slate-400 hover:text-white mt-2"
          >
            {showClear ? "Hide" : "Show"} the {x.clear.length} rules that passed
          </button>
          {showClear && (
            <ul className="mt-2 space-y-1">
              {x.clear.map((c) => (
                <li key={c.key} className="flex items-baseline justify-between gap-3 text-xs">
                  <span className="text-slate-400">
                    {c.label}
                    <span className="block text-[11px] text-slate-600">
                      read {c.reading} · rule: {c.threshold}
                    </span>
                  </span>
                  <span className="text-emerald-400/80 tabular-nums shrink-0">−0</span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      <p className="text-[11px] text-slate-500 mt-3">{x.note}</p>
      {scored && <p className="text-[11px] text-slate-600 mt-1">Bands: {x.band_rule}</p>}
    </div>
  );
}
