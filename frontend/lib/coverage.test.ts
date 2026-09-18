import { describe, expect, it } from "vitest";

import { coveragePhrase } from "./coverage";

/**
 * The one wording for a partial plant OEE. These are the exact strings the
 * backend's oee_contract.coverage_phrase returns for the same inputs
 * (backend test_scorecard_oee_states_coverage.py section 5), so a tile and the
 * briefing beside it can never word it two ways.
 */
const cov = (reporting: number, expected: number, complete = reporting === expected && expected > 0) => ({
  machines_expected: expected, machines_reporting: reporting,
  coverage_pct: expected ? Math.round((reporting / expected) * 100) : null, complete,
});

describe("coveragePhrase", () => {
  it("names a partial figure", () => {
    expect(coveragePhrase(cov(2, 3))).toBe("from 2 of 3 machines");
  });

  it("is singular for a one-machine plant", () => {
    expect(coveragePhrase(cov(0, 1))).toBe("from 0 of 1 machine");
  });

  it("says nothing when every machine reported", () => {
    expect(coveragePhrase(cov(3, 3))).toBe("");
  });

  it("says nothing with no coverage, or no machines to speak of", () => {
    expect(coveragePhrase(null)).toBe("");
    expect(coveragePhrase(undefined)).toBe("");
    expect(coveragePhrase(cov(0, 0, false))).toBe("");
  });
});
