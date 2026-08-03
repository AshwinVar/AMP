import { describe, expect, it } from "vitest";

import { barPct } from "./bar";

/**
 * The cost-of-losses card (CostSnapshot) drew its daily loss series and its
 * downtime/scrap breakdown with `Math.max(floor, round((value / peak) * 100))`,
 * with no zero guard. The loss read-model returns a DENSE 7-day series (every
 * day in the window, including days with no downtime and no scrap -> cost 0) and
 * ALWAYS returns both loss components (downtime AND scrap, even at 0). So a
 * zero-loss day, or a week with zero scrap, painted a ~2-3% red bar reading as a
 * loss that never happened.
 *
 * That is the same phantom-bar-on-a-zero-bucket bug the daily-series audit (#464)
 * fixed on the count-based sparklines (`value === 0 ? 0 : Math.max(floor, ...)`);
 * CostSnapshot was simply missed. Cost of losses is the money twin of downtime
 * minutes (which #464 DID guard): a 0 there is unambiguous -- nothing was lost --
 * so the honest render is no bar, never a floored sliver.
 *
 * Numbers below are derived by hand from `min(100, max(floor, round(value/peak*100)))`.
 */
describe("barPct", () => {
  it("renders a zero bucket at 0, never the floor (the phantom-bar bug)", () => {
    expect(barPct(0, 100, 3)).toBe(0); // a zero-loss day: no bar, not a 3% sliver
    expect(barPct(0, 5, 2)).toBe(0); // a zero-cost loss component: empty, not 2%
    expect(barPct(0, 1, 3)).toBe(0);
  });

  it("keeps the floor for a tiny-but-nonzero value so it stays visible", () => {
    // 1/1000 -> round(0.1) = 0, floored up to 3.
    expect(barPct(1, 1000, 3)).toBe(3);
    // 1/1000 with floor 2.
    expect(barPct(1, 1000, 2)).toBe(2);
  });

  it("computes an exact proportion above the floor", () => {
    expect(barPct(50, 100, 3)).toBe(50);
    expect(barPct(2, 3, 0)).toBe(67); // round(66.66..) = 67
    expect(barPct(40, 100)).toBe(40); // default floor 0
  });

  it("caps at 100 and never exceeds the track", () => {
    expect(barPct(1000, 1000, 3)).toBe(100);
    expect(barPct(150, 100, 2)).toBe(100); // over-peak clamps, not 150%
  });

  it("is safe on a non-positive or non-finite input (no NaN%, no divide-by-zero)", () => {
    expect(barPct(5, 0, 3)).toBe(0); // peak 0 (empty window) -> 0, not Infinity
    expect(barPct(-5, 100, 3)).toBe(0); // a stray negative -> 0
    expect(barPct(Number.NaN, 100, 3)).toBe(0); // a bad reading -> 0, not NaN%
    expect(barPct(5, Number.NaN, 3)).toBe(0);
  });
});
