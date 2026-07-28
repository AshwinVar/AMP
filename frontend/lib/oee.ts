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
 * The estimate. Deliberately identical to
 * `backend/analytics_engine.py:calculate_fallback_oee`, which the API uses for
 * the same "no production data" case — the two must not drift.
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
  if (measured && typeof measured.oee === "number") {
    // A measured 0 is a real reading (a dead machine), not missing data.
    return { oee: measured.oee, measured: true };
  }

  // `Machine.utilization` is a nullable Integer column. With no measurement and
  // no reading there is nothing honest to print, so say nothing rather than
  // guessing 0%.
  if (typeof utilization !== "number" || !Number.isFinite(utilization)) return null;

  return { oee: fallbackOee(utilization), measured: false };
}
