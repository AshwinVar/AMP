import { describe, expect, it } from "vitest";
import {
  CAUSE_LABELS,
  DATA_STATES,
  PROVENANCE,
  PROVENANCE_MEANING,
  REFUSALS,
  formatFactValue,
  isRefusal,
  provenanceTone,
  stateNotice,
} from "./evidence";
import { CURRENCY } from "./money";

/**
 * The Copilot's evidence vocabulary (ADR-0022). The lists themselves are checked
 * against backend/ai/evidence.py by backend/test_copilot_evidence_vocabulary.py;
 * this file pins how the UI reads them.
 */
describe("the vocabulary", () => {
  it("has a meaning for every provenance and a tone that is not 'unknown' except for UNKNOWN", () => {
    for (const p of PROVENANCE) {
      expect(PROVENANCE_MEANING[p]).toBeTruthy();
      expect(provenanceTone(p) === "unknown").toBe(p === "UNKNOWN");
    }
  });

  it("never calls a rule-based assessment machine learning", () => {
    expect(PROVENANCE_MEANING["RULE-BASED ASSESSMENT"]).toMatch(/not machine learning/i);
  });

  it("reads an unrecognised provenance as unknown rather than as a measurement", () => {
    expect(provenanceTone("VIBES")).toBe("unknown");
  });

  it("gives every non-OK state and every refusal a notice, and OK none", () => {
    expect(stateNotice("OK")).toBeNull();
    for (const s of DATA_STATES.filter((x) => x !== "OK")) expect(stateNotice(s)).toBeTruthy();
    for (const s of REFUSALS) expect(stateNotice(s)).toBeTruthy();
    expect(stateNotice(undefined)).toBeNull();
  });

  it("knows the refusals apart from the data states", () => {
    for (const s of REFUSALS) expect(isRefusal(s)).toBe(true);
    for (const s of DATA_STATES) expect(isRefusal(s)).toBe(false);
  });

  it("has the four root-cause labels", () => {
    expect(CAUSE_LABELS).toEqual(["CAUSE CONFIRMED", "LIKELY CONTRIBUTOR", "CORRELATED EVENT", "INSUFFICIENT EVIDENCE"]);
  });
});

describe("formatFactValue", () => {
  it("prints money only when the unit is the platform currency", () => {
    expect(formatFactValue({ value: 49740, unit: CURRENCY })).toBe(`${CURRENCY}${(49740).toLocaleString()}`);
    expect(formatFactValue({ value: 1674, unit: "units" })).toBe(`${(1674).toLocaleString()} units`);
  });

  it("says unknown for a missing figure, never zero or a blank", () => {
    expect(formatFactValue({ value: null, unit: CURRENCY })).toBe("unknown");
    expect(formatFactValue({ value: Number.NaN, unit: "%" })).toBe("unknown");
  });

  it("formats percentages, minutes, scores and names", () => {
    expect(formatFactValue({ value: 83, unit: "%" })).toBe("83%");
    expect(formatFactValue({ value: 45, unit: "min" })).toBe("45 min");
    expect(formatFactValue({ value: 72, unit: "/100" })).toBe("72/100");
    expect(formatFactValue({ value: "CNC-01", unit: "" })).toBe("CNC-01");
    expect(formatFactValue({ value: 0, unit: "events" })).toBe("0 events");
  });
});
