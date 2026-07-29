/**
 * Shift attainment, on the same basis the backend uses.
 *
 * A shift with `target_output = 0` is a legitimate UNPLANNED shift — overtime,
 * a callout, a line brought up to clear a backlog. machines_routes.py:152-155
 * accepts it deliberately ("A target of 0 is a legitimate UNPLANNED shift ...
 * so only negatives are rejected — never zero"), so it is a value the dashboard
 * must handle, not an edge case it can wish away.
 *
 * The dashboard got it wrong in two different directions at once:
 *
 *   const avgShiftEfficiency = shifts.reduce((sum, shift) => {
 *     if (shift.target_output === 0) return sum;          // dropped from the TOP
 *     return sum + (shift.actual_output / shift.target_output) * 100;
 *   }, 0) / shifts.length                                 // but still in the BOTTOM
 *
 * Given two shifts that each hit exactly 100% of target plus one unplanned
 * shift, the headline KPI read 67% while the shift table on the same screen
 * read 100%, 100%, 0% and every backend surface pooled to 104%. The error
 * scales with the share of unplanned shifts: /shifts returns up to 100 rows, so
 * 10 planned shifts all at 100% of target alongside 90 unplanned ones printed
 * "Avg Shift Eff. 10%" for a plant running exactly to plan.
 *
 * Pooling — one ratio of the summed totals — fixes that and a second fault the
 * backend comment (analytics_routes.py:115-125) also names: a mean of ratios
 * over-weights a small shift, so a 10/10 and a 900/1000 averaged to 95% when
 * the plant really made 910 of 1010 = 90%. Pooling adds an unplanned shift's
 * real output to the numerator and nothing to the denominator, so it neither
 * fabricates a 0% nor disagrees with the per-shift rows beside it.
 *
 * Ported from ai/shift.py `_attainment` and pinned to it by lib/shift.test.ts,
 * the same way lib/duration.ts is pinned to backend/duration.py (#394) and
 * lib/oee.ts to analytics_engine (#396).
 */

import { roundHalfToEven } from "./duration";

export type ShiftLike = {
  target_output: number;
  actual_output: number;
};

/**
 * Actual as a percentage of target, or **null** when there is no target to
 * measure against.
 *
 * null rather than 0 is the point. ai/shift.py:18-22 spells out why: "A shift
 * with no planned output has no attainment — scoring it 0% would flag an
 * unplanned shift as the 'worst' performer (ADR-0007: never present a metric
 * the data can't support)."
 *
 * Rounded half-to-even, because the backend's `round()` is Python's. Not
 * pedantry: 1 made against a target of 40 is exactly 2.5%, and Math.round would
 * print 3% next to an API that says 2%.
 */
export function shiftAttainment(actual: number, target: number): number | null {
  return target ? roundHalfToEven((actual / target) * 100) : null;
}

/**
 * The plant's attainment across a set of shifts: one ratio of the summed
 * totals, matching analytics_routes.py's `avg_shift_efficiency` and
 * ai/shift.py's top-level `attainment`.
 *
 * Returns null when nothing in the set had a target — no plan, no attainment.
 */
export function pooledShiftEfficiency(shifts: readonly ShiftLike[]): number | null {
  let target = 0;
  let actual = 0;
  for (const s of shifts) {
    target += s.target_output || 0;
    actual += s.actual_output || 0;
  }
  return shiftAttainment(actual, target);
}
