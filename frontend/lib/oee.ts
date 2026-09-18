import type { ExecutiveMachineOee } from "./phase15-types";

/**
 * Which OEE a machine card should show.
 *
 * OEE is availability x performance x quality. The card used to compute
 * `utilization / 100 * 0.9 * 0.95 * 100` — performance and quality were
 * constants, so the figure was a linear rescale of the utilization printed
 * directly above it and could not distinguish a machine scrapping 40% of its
 * output from one scrapping nothing.
 *
 * `/analytics/executive-oee` already returns measured availability, performance
 * and quality per machine, derived from production records, quality inspections
 * and parsed downtime. Prefer it. The estimate stays only for machines with no
 * production data at all, where it is the honest best available — and the
 * caller is told, via `measured`, so it can label it.
 */

export type MachineOeeReading = {
  oee: number;
  /** false means this is the estimate, and the card must say so. */
  measured: boolean;
};

/**
 * The estimate, shown only as "Estimated OEE". It is the last OEE estimate in
 * AMP: the backend's copy (`analytics_engine.calculate_fallback_oee`) published
 * it unlabelled as the plant OEE of a week with no production, and was removed
 * (backend `test_summary_never_invents_oee.py`).
 */
export function fallbackOee(utilization: number) {
  return Math.round((utilization / 100) * 0.9 * 0.95 * 100);
}

export function readMachineOee(
  machineId: number,
  ranking: ExecutiveMachineOee[] | null | undefined,
  utilization: number | null | undefined,
): MachineOeeReading | null {
  const measured = ranking?.find((row) => row.machine_id === machineId);
  // Only a row the backend STATES was measured. This used to accept any row in
  // the ranking, and the backend returns a row for every machine — including
  // ones it filled with constants (utilization, 90/60, 95) when they produced
  // nothing — so an idle machine's invented 68% reached this card labelled as a
  // measurement, and the honest estimate below never ran. `=== true` rather than
  // `!== false`: a payload without the field is not evidence of a measurement.
  if (measured && measured.measured === true && typeof measured.oee === "number") {
    // A measured 0 is a real reading (a dead machine), not missing data.
    return { oee: measured.oee, measured: true };
  }

  // `Machine.utilization` is a nullable Integer column. With no measurement and
  // no reading there is nothing honest to print, so say nothing rather than
  // guessing 0%.
  if (typeof utilization !== "number" || !Number.isFinite(utilization)) return null;

  return { oee: fallbackOee(utilization), measured: false };
}
