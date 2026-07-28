import { describe, expect, it } from "vitest";

import { parseDurationToMinutes } from "./duration";

/**
 * `downtime_logs.duration` is a free-text String column - operators type
 * "2 hrs 15 min", "45 min", a bare "90", or a decimal "1.5 hrs" by hand. Two
 * implementations read it: the backend's `duration.parse_duration_to_minutes`,
 * which feeds OEE, the cost-of-losses figure, the reliability and predictive
 * risk scores; and the frontend, which renders the dashboard's Total Downtime
 * tile from the same rows.
 *
 * The backend was fixed to match a decimal number (`\d+(?:\.\d+)?`). An
 * integer-only `\d+` skips the "1." and matches the "5" in "1.5 h", reading a
 * 90-minute stop as 5 hours. The frontend still carried the pre-fix regex, so
 * for any decimal entry the tile and the API disagreed by 3x on the same data -
 * and the tile was the wrong one.
 *
 * The table below is not hand-written: every expectation is the value the
 * backend actually returns, so this file is a parity oracle. If the two drift
 * again, this goes red.
 */

// input -> minutes, as produced by backend/duration.py
const PARITY: [string, number][] = [
  ["25 min", 25],
  ["45 min", 45],
  ["90", 90],
  ["2 hrs 15 min", 135],
  ["1 hr 10 min", 70],
  ["1h 30m", 90],
  ["2h", 120],
  ["1 hour", 60],
  ["30 minutes", 30],
  ["10 m", 10],
  ["1 hr", 60],
  ["0", 0],
  // Decimals - the case the pre-fix regex got wrong.
  ["1.5 hrs", 90],
  ["0.5 h", 30],
  ["2.5h", 150],
  ["0.25 hr", 15],
  ["3.75 hrs", 225],
  ["1.5", 2],
  // Nothing parseable.
  ["", 0],
  ["   ", 0],
  ["n/a", 0],
  ["abc", 0],
];

describe("parseDurationToMinutes", () => {
  for (const [input, expected] of PARITY) {
    it(`${JSON.stringify(input)} -> ${expected} min, same as the backend`, () => {
      expect(parseDurationToMinutes(input)).toBe(expected);
    });
  }

  it("reads a decimal hour as its real length, not the digit after the point", () => {
    // The headline case. "1.5 hrs" is 90 minutes. The old regex found "5h" and
    // returned 300 - a 3x overstatement that landed on the dashboard tile while
    // every backend rollup of the same log said 90.
    expect(parseDurationToMinutes("1.5 hrs")).toBe(90);
    expect(parseDurationToMinutes("0.5 h")).toBe(30);
  });

  it("does not concatenate the digits of an unlabelled decimal", () => {
    // The old fallback stripped every non-digit and glued what was left
    // together, so a bare "1.5" became "15" - fifteen minutes for a 90-second
    // stop. The backend reads the first number it finds.
    expect(parseDurationToMinutes("1.5")).toBe(2);
  });

  it("survives a null or undefined duration without throwing", () => {
    // The column is NOT NULL, but the tile sums whatever the API returns and a
    // throw here blanks the whole dashboard.
    expect(parseDurationToMinutes(null)).toBe(0);
    expect(parseDurationToMinutes(undefined)).toBe(0);
  });

  it("rounds halves the way Python does, so totals agree to the minute", () => {
    // Python's round() is half-to-even: round(2.5) is 2, not 3. JavaScript's
    // Math.round(2.5) is 3. Only ties diverge, and only by a minute, but a tile
    // that says 3 where the API says 2 is the same class of bug as the 3x one.
    expect(parseDurationToMinutes("2.5 m")).toBe(2);
    expect(parseDurationToMinutes("1.5 m")).toBe(2);
    expect(parseDurationToMinutes("0.5 m")).toBe(0);
    expect(parseDurationToMinutes("3.5 m")).toBe(4);
  });
});
