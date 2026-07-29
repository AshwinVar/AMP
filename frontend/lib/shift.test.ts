import { describe, expect, it } from "vitest";

import { pooledShiftEfficiency, shiftAttainment, type ShiftLike } from "./shift";

/**
 * The reference oracle is the shipped Python, transcribed:
 *
 *   ai/shift.py           _attainment(actual, target)
 *                           -> round(actual / target * 100) if target else None
 *   analytics_routes.py   avg_shift_efficiency
 *                           -> round((total_actual / total_target) * 100) if total_target else 0
 *
 * Both are a ratio of SUMS. The dashboard shipped a mean of per-shift RATIOS
 * divided by every row including the ones it had just excluded from the top of
 * the fraction, so it disagreed with both — and with the table rendered
 * directly beneath it.
 */

const shift = (target: number, actual: number): ShiftLike => ({
  target_output: target,
  actual_output: actual,
});

/** analytics_routes.py:141, transcribed. */
function backendPooled(shifts: readonly ShiftLike[]): number {
  const target = shifts.reduce((a, s) => a + s.target_output, 0);
  const actual = shifts.reduce((a, s) => a + s.actual_output, 0);
  return target ? Math.round((actual / target) * 100) : 0;
}

/** The formula that shipped, verbatim from page.tsx:1784-1792. */
function oldFrontend(shifts: readonly ShiftLike[]): number {
  return shifts.length > 0
    ? Math.round(
        shifts.reduce((sum, s) => {
          if (s.target_output === 0) return sum;
          return sum + (s.actual_output / s.target_output) * 100;
        }, 0) / shifts.length,
      )
    : 0;
}

describe("the bug", () => {
  it("understated the headline whenever a shift was unplanned", () => {
    // Two shifts at exactly 100% of target, plus one unplanned overtime shift.
    const shifts = [shift(500, 500), shift(500, 500), shift(0, 40)];

    expect(oldFrontend(shifts)).toBe(67); // what the KPI card showed
    expect(pooledShiftEfficiency(shifts)).toBe(104); // what it should show
    expect(pooledShiftEfficiency(shifts)).toBe(backendPooled(shifts));
  });

  it("got worse the more unplanned shifts there were", () => {
    // /shifts returns up to 100 rows (machines_routes.py:136). A plant running
    // exactly to plan, with most of its rows unplanned, read as a catastrophe.
    const shifts = [
      ...Array.from({ length: 10 }, () => shift(500, 500)),
      ...Array.from({ length: 90 }, () => shift(0, 5)),
    ];

    expect(oldFrontend(shifts)).toBe(10); // "Avg Shift Eff. 10%"
    expect(pooledShiftEfficiency(shifts)).toBe(109);
  });

  it("also over-weighted a small shift, with no unplanned rows in sight", () => {
    // The second fault named at analytics_routes.py:118-120. Nothing here has a
    // zero target, so this one is pure mean-of-ratios error.
    const shifts = [shift(10, 10), shift(1000, 900)];

    expect(oldFrontend(shifts)).toBe(95); // (100 + 90) / 2
    expect(pooledShiftEfficiency(shifts)).toBe(90); // 910 of 1010
  });
});

describe("pooledShiftEfficiency agrees with the backend", () => {
  const cases: { name: string; shifts: ShiftLike[] }[] = [
    { name: "a single shift under target", shifts: [shift(500, 400)] },
    { name: "a single shift over target", shifts: [shift(500, 620)] },
    { name: "several ordinary shifts", shifts: [shift(480, 455), shift(480, 501), shift(300, 288)] },
    { name: "an unplanned shift among planned ones", shifts: [shift(500, 500), shift(0, 40)] },
    { name: "several unplanned shifts", shifts: [shift(0, 12), shift(600, 590), shift(0, 3)] },
    { name: "a shift that produced nothing", shifts: [shift(500, 0), shift(500, 500)] },
    { name: "wildly different shift sizes", shifts: [shift(5, 5), shift(5000, 4000)] },
  ];

  for (const { name, shifts } of cases) {
    it(name, () => {
      expect(pooledShiftEfficiency(shifts)).toBe(backendPooled(shifts));
    });
  }
});

describe("shiftAttainment", () => {
  it("is a plain percentage when there is a target", () => {
    expect(shiftAttainment(450, 500)).toBe(90);
    expect(shiftAttainment(500, 500)).toBe(100);
    expect(shiftAttainment(620, 500)).toBe(124); // over-delivery is not clamped
  });

  it("is null — not 0 — for an unplanned shift", () => {
    // ai/shift.py:18-22: "scoring it 0% would flag an unplanned shift as the
    // 'worst' performer (ADR-0007: never present a metric the data can't
    // support)". The table used to print a fabricated 0% here.
    expect(shiftAttainment(40, 0)).toBeNull();
    expect(shiftAttainment(0, 0)).toBeNull();
  });

  it("rounds the way Python's round does for these inputs", () => {
    // round(1/3*100) == 33, round(2/3*100) == 67 in both languages.
    expect(shiftAttainment(1, 3)).toBe(33);
    expect(shiftAttainment(2, 3)).toBe(67);
  });

  it("breaks a tie to even, like the round() it is ported from", () => {
    // Found by running the real ai/shift._attainment beside this one rather than
    // trusting the transcription. These land on exactly x.5, where Python's
    // banker's rounding and Math.round part company:
    //
    //   actual/target      exact %   python  Math.round
    //   1 / 40               2.5        2        3
    //   5 / 200              2.5        2        3
    //   3 / 120              2.5        2        3
    //
    // A one-point disagreement between a card and the API it claims to mirror is
    // exactly the class of drift lib/duration.ts was written to end (#394).
    expect(shiftAttainment(1, 40)).toBe(2);
    expect(shiftAttainment(5, 200)).toBe(2);
    expect(shiftAttainment(3, 120)).toBe(2);
    // ...and 3.5 rounds UP to 4, because 4 is the even one. Not "always down".
    expect(shiftAttainment(7, 200)).toBe(4);
  });
});

describe("the empty and degenerate cases", () => {
  it("returns null for no shifts at all", () => {
    expect(pooledShiftEfficiency([])).toBeNull();
  });

  it("returns null when every shift is unplanned", () => {
    // No plan, no attainment. The card renders a dash rather than inventing 0%.
    expect(pooledShiftEfficiency([shift(0, 10), shift(0, 25)])).toBeNull();
  });

  it("counts an unplanned shift's real output toward the numerator", () => {
    // This is what makes the pooled figure exceed 100% in the headline case,
    // and it is correct: the plant genuinely made more than it planned.
    expect(pooledShiftEfficiency([shift(100, 100), shift(0, 50)])).toBe(150);
  });

  it("survives a missing quantity rather than blanking the whole card", () => {
    // A row that arrives without the key at all — a legacy or partial payload.
    // Note this uses `undefined`, not `null`, and the distinction is the whole
    // test: `0 + null` is 0 in JS, so a null costs nothing and a fixture built
    // from nulls passes even with the `|| 0` removed (mutation testing caught
    // exactly that). `0 + undefined` is NaN, and NaN is falsy, so the guard in
    // shiftAttainment would return null and the KPI would show "—" for a plant
    // whose other shifts are perfectly measurable.
    const missingKey = [{ actual_output: 40 }, shift(500, 400)] as unknown as ShiftLike[];
    expect(pooledShiftEfficiency(missingKey)).toBe(88);

    const nulls = [{ target_output: null, actual_output: 40 }, shift(500, 400)] as unknown as ShiftLike[];
    expect(pooledShiftEfficiency(nulls)).toBe(88);
  });
});
