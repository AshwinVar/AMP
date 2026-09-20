"use client";

import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { stateNotice } from "../lib/evidence";

// Mirrors ai/outcomes.py (ADR-0029).
type Outcome = {
  id: number;
  action_id: number;
  action: string | null;
  agent: string | null;
  metric_label: string;
  unit: string;
  rule: string;
  better: "lower" | "higher";
  scope_label: string | null;
  window_days: number;
  baseline_value: number | null;
  measured_value: number | null;
  change: number | null;
  waiting: boolean;
  days_left: number;
  verdict: string | null;
};
type Summary = {
  state: string;
  headline: string;
  window_days: number;
  followed_up: number;
  waiting: number;
  measured: number;
  counts: Record<string, number>;
  outcomes: Outcome[];
  note: string;
};

/** What a reading is, as this card may state it. "no reading" is not "0". */
export function reading(value: number | null, unit: string): string {
  if (value === null) return "no reading";
  const shown = Number.isInteger(value) ? value.toLocaleString() : value.toFixed(1);
  return unit ? `${shown} ${unit}` : shown;
}

const VERDICT_CLASS: Record<string, string> = {
  BETTER: "text-emerald-300 border-emerald-500/40",
  WORSE: "text-red-300 border-red-500/40",
  "NO CHANGE": "text-slate-400 border-slate-600",
  "NOT MEASURABLE": "text-slate-500 border-slate-700",
};

/**
 * Did it help? (ADR-0029)
 *
 * The first thing AMP has ever shown about its OWN recommendations after the
 * fact — and the easiest place in the product to overclaim. Three rules hold it
 * down, and each is asserted in the tests:
 *
 *   * the verdict word describes THE METRIC, and the row says which metric and
 *     over what rule, so "BETTER" can never float free of what got better;
 *   * nothing inside its window is given a verdict at all, not a provisional
 *     one — it says how many days are left;
 *   * the caveat is on the card, above the list, never behind a toggle: AMP
 *     measured what changed, it did not show the action caused it.
 */
export default function ActionOutcomesSection() {
  const [s, setS] = useState<Summary | null>(null);

  const load = useCallback(async () => {
    try {
      setS(await apiGet<Summary>("/action-outcomes"));
    } catch {
      // A glanceable card — stay quiet rather than break the page.
    }
  }, []);
  useEffect(() => {
    let alive = true;
    const first = setTimeout(() => { if (alive) load(); }, 0);
    return () => { alive = false; clearTimeout(first); };
  }, [load]);

  if (!s) return null;
  const notice = stateNotice(s.state);
  return (
    <section className="rounded-2xl bg-slate-900 border border-slate-800 p-5">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-xl font-bold">Did it help?</h2>
          <p className="text-slate-300 text-sm mt-1">{s.headline}</p>
        </div>
        <span className="text-[11px] text-slate-500 shrink-0">
          {s.window_days} days each side of the decision
        </span>
      </div>
      {notice && <p role="status" className="text-amber-300/90 text-xs mt-2">{s.state} · {notice}</p>}

      {/* The caveat sits ABOVE the numbers, not under them. */}
      <p className="text-[11px] text-amber-300/90 mt-3 rounded-lg border border-amber-500/20 bg-amber-500/5 p-2">
        {s.note}
      </p>

      {s.outcomes.length === 0 ? (
        <p className="text-slate-400 text-sm mt-3">
          Nothing to show yet. AMP starts measuring the moment an action is approved.
        </p>
      ) : (
        <ul className="mt-3 space-y-2">
          {s.outcomes.map((o) => (
            <li key={o.id} className="rounded-xl bg-slate-950/60 border border-slate-800 p-3">
              <div className="flex items-start justify-between gap-2 flex-wrap">
                <div>
                  <p className="text-sm text-slate-200">{o.action ?? `Action ${o.action_id}`}</p>
                  <p className="text-xs text-slate-500">
                    {o.metric_label}
                    {o.scope_label ? ` on ${o.scope_label}` : ""} · {o.rule}
                  </p>
                </div>
                <div className="text-right shrink-0">
                  {o.waiting ? (
                    // No verdict inside the window. Not even a provisional one.
                    <span className="text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border text-slate-400 border-slate-600">
                      {o.days_left === 0 ? "due now" : `${o.days_left}d to go`}
                    </span>
                  ) : (
                    <span
                      className={`text-[10px] uppercase tracking-wide rounded-full px-2 py-0.5 border ${
                        VERDICT_CLASS[o.verdict ?? ""] ?? VERDICT_CLASS["NOT MEASURABLE"]
                      }`}
                    >
                      {o.verdict}
                    </span>
                  )}
                </div>
              </div>
              <p className="text-xs text-slate-400 mt-2 tabular-nums">
                before {reading(o.baseline_value, o.unit)}
                {o.waiting ? (
                  <span className="text-slate-600"> · measured after the window</span>
                ) : (
                  <>
                    {" → after "}
                    {reading(o.measured_value, o.unit)}
                    {o.change !== null && (
                      <span className="text-slate-500">
                        {" ("}
                        {o.change > 0 ? "+" : ""}
                        {o.change} {o.unit}, {o.better === "lower" ? "lower is better" : "higher is better"}
                        {")"}
                      </span>
                    )}
                  </>
                )}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
