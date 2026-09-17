import { describe, expect, it } from "vitest";

import {
  compareToBaseline,
  consentStateText,
  copilotBadge,
  dataLabel,
  describeAnomaly,
  describeAnomalyError,
  formatDifference,
  formatInterval,
  formatMetricValue,
  modelIdentity,
  verdictBadge,
  type ConsentCapability,
  type HeadlineRow,
} from "./aiModels";

/**
 * AMP-native AI wording (ADR-0020).
 *
 * What is pinned is that the screen cannot claim more than the evaluation did:
 * a difference whose interval includes zero is "no clear difference", an
 * unavailable model is not "experimental", a missing metric is "—" and never 0,
 * and an experimental anomaly score is never an alert.
 */

function row(over: Partial<HeadlineRow> = {}): HeadlineRow {
  return {
    metric: "PR-AUC",
    dataset: "held-out synthetic test machines",
    unit: "score",
    higher_is_better: true,
    model: 0.1937,
    model_lo: 0.1032,
    model_hi: 0.3061,
    baseline_name: "rule-based risk score",
    baseline: 0.0943,
    baseline_lo: 0.0549,
    baseline_hi: 0.1393,
    difference: 0.0994,
    difference_lo: 0.0294,
    difference_hi: 0.1749,
    ...over,
  };
}

function cap(over: Partial<ConsentCapability> = {}): ConsentCapability {
  return {
    capability: "telemetry_baseline",
    title: "Learn each machine's normal telemetry",
    reads: "r",
    stored: "s",
    used_by: "u",
    without: "w",
    granted: false,
    granted_by: null,
    granted_at: null,
    revoked_by: null,
    revoked_at: null,
    updated_at: null,
    ...over,
  };
}

describe("numbers", () => {
  it("formats scores to three places and proportions as percentages", () => {
    expect(formatMetricValue(0.19374, "score")).toBe("0.194");
    expect(formatMetricValue(0.0155, "percent")).toBe("1.6%");
    expect(formatMetricValue(0.5)).toBe("0.500");
  });

  it("shows a missing metric as a dash, never as zero", () => {
    expect(formatMetricValue(null)).toBe("—");
    expect(formatMetricValue(undefined, "percent")).toBe("—");
    expect(formatMetricValue(Number.NaN)).toBe("—");
    expect(formatDifference(null)).toBe("—");
  });

  it("signs differences, in points for proportions", () => {
    expect(formatDifference(0.0994)).toBe("+0.099");
    expect(formatDifference(-0.103)).toBe("−0.103");
    expect(formatDifference(0)).toBe("±0.000");
    expect(formatDifference(0.03125, "percent")).toBe("+3.1 pts");
  });

  it("prints an interval only when both ends exist", () => {
    expect(formatInterval(0.1032, 0.3061)).toBe("[0.103, 0.306]");
    expect(formatInterval(-0.03125, 0.09375, "percent", true)).toBe("[−3.1 pts, +9.4 pts]");
    expect(formatInterval(null, 0.3)).toBe("");
    expect(formatInterval(0.1, undefined)).toBe("");
  });
});

describe("compareToBaseline", () => {
  it("claims better only when the paired interval is above zero", () => {
    const c = compareToBaseline(row());
    expect(c).toMatchObject({ verdict: "better", fromInterval: true });
    expect(c.text).toContain("rule-based risk score");
  });

  it("says worse when the interval is below zero (the anomaly scorer vs mean/std)", () => {
    const c = compareToBaseline(row({ difference: -0.103, difference_lo: -0.158, difference_hi: -0.047 }));
    expect(c.verdict).toBe("worse");
  });

  it("does not call a difference whose interval includes zero an improvement", () => {
    const c = compareToBaseline(
      row({ unit: "percent", model: 0.594, baseline: 0.5625, difference: 0.031, difference_lo: -0.031, difference_hi: 0.094 }),
    );
    expect(c).toMatchObject({ verdict: "unclear", fromInterval: true });
    expect(c.text).toMatch(/no clear difference/i);
  });

  it("flips the direction for a lower-is-better metric", () => {
    expect(
      compareToBaseline(row({ higher_is_better: false, difference_lo: 0.01, difference_hi: 0.02 })).verdict,
    ).toBe("worse");
    expect(
      compareToBaseline(row({ higher_is_better: false, difference_lo: -0.02, difference_hi: -0.01 })).verdict,
    ).toBe("better");
  });

  it("falls back to point estimates, and says so, when there is no interval", () => {
    const noCi = { difference: null, difference_lo: null, difference_hi: null };
    const lowerAlarms = compareToBaseline(
      row({ ...noCi, higher_is_better: false, unit: "percent", model: 0.0155, baseline: 0.0178 }),
    );
    expect(lowerAlarms).toMatchObject({ verdict: "better", fromInterval: false });
    expect(lowerAlarms.text).toMatch(/point estimate only/);
    expect(compareToBaseline(row({ ...noCi, model: 0.3, baseline: 0.4 })).verdict).toBe("worse");
    expect(compareToBaseline(row({ ...noCi, model: 0.4, baseline: 0.4 })).verdict).toBe("equal");
    expect(compareToBaseline(row({ ...noCi, model: Number.NaN })).verdict).toBe("unclear");
  });

  it("needs both ends of an interval before trusting it", () => {
    expect(compareToBaseline(row({ difference_lo: 0.01, difference_hi: null, model: 0.1, baseline: 0.2 })).verdict).toBe(
      "worse",
    );
  });
});

describe("verdicts", () => {
  it("never rounds an unavailable or unadopted model up", () => {
    expect(verdictBadge({ available: true, adopted: true })).toEqual({ label: "Adopted", tone: "adopted" });
    expect(verdictBadge({ available: true, adopted: false }).tone).toBe("experimental");
    expect(verdictBadge({ available: true, adopted: null }).tone).toBe("experimental");
    expect(verdictBadge({ available: false, adopted: true })).toEqual({ label: "Unavailable", tone: "unavailable" });
  });

  it("identifies a model by version and pinned hash", () => {
    expect(
      modelIdentity({ model_name: "failure_risk", version: "v1", sha256: "ad30970f3ac1", sha256_pinned: "x" }),
    ).toBe("failure_risk@v1 · sha256 ad30970f…");
    expect(modelIdentity({ model_name: null, version: null, sha256: null, sha256_pinned: "69e825ef00" })).toBe(
      "sha256 69e825ef…",
    );
    expect(modelIdentity({ model_name: null, version: null, sha256: null, sha256_pinned: "" })).toBe("");
  });
});

describe("copilotBadge", () => {
  it("names the LLM when one answered", () => {
    expect(copilotBadge({ source: "llm", model: "claude-haiku-4-5" })).toEqual({
      text: "✦ AI · claude-haiku-4-5",
      tone: "llm",
    });
    expect(copilotBadge({ source: "llm" }).text).toBe("✦ AI · model");
  });

  it("shows AMP's own model only when it chose the route, without an uncalibrated percentage", () => {
    const badge = copilotBadge({ source: "rules", route_source: "model", confidence: 0.912 });
    expect(badge).toEqual({ text: "AMP native · model-routed", tone: "native" });
    expect(badge.text).not.toMatch(/\d/);
    expect(copilotBadge({ source: "rules", route_source: "model", confidence: 0.59 }).text).not.toMatch(/%/);
    expect(copilotBadge({ source: "rules", route_source: "model", confidence: null }).tone).toBe("rules");
  });

  it("labels everything else as the rules", () => {
    expect(copilotBadge({ source: "rules", route_source: "keywords" })).toEqual({ text: "instant · rules", tone: "rules" });
    expect(copilotBadge({}).tone).toBe("rules");
  });
});

describe("dataLabel", () => {
  it("says what each model was trained and evaluated on, in its own words", () => {
    expect(dataLabel({ name: "failure_risk" })).toBe("Trained and evaluated on synthetic data only");
    expect(dataLabel({ name: "copilot_intent" })).toBe("Trained and evaluated on AMP-authored questions only");
    const anomaly = dataLabel({ name: "telemetry_anomaly" });
    expect(anomaly).toMatch(/^Evaluated on synthetic data only/);
    expect(anomaly).toMatch(/own telemetry/);
    expect(anomaly).not.toMatch(/^Trained/);
  });

  it("claims nothing for a model it does not know", () => {
    expect(dataLabel({ name: "something_new" })).not.toMatch(/synthetic/);
  });
});

describe("consentStateText", () => {
  it("says who turned it on", () => {
    const text = consentStateText(cap({ granted: true, granted_by: "ta-admin", granted_at: "2026-09-17T10:00:00" }));
    expect(text).toMatch(/^On · turned on by ta-admin on /);
    expect(consentStateText(cap({ granted: true }))).toBe("On · turned on by an Admin");
  });

  it("says who turned it off", () => {
    expect(consentStateText(cap({ revoked_by: "ta-admin-2", revoked_at: "2026-09-18T10:00:00" }))).toMatch(
      /^Off · turned off by ta-admin-2 on /,
    );
    expect(consentStateText(cap({ revoked_by: "ta-admin-2" }))).toBe("Off · turned off by ta-admin-2");
    expect(consentStateText(cap({ revoked_at: "2026-09-18T10:00:00" }))).toMatch(/^Off · turned off by an Admin on /);
  });

  it("distinguishes never granted from withdrawn", () => {
    expect(consentStateText(cap())).toBe("Off · never turned on");
  });
});

describe("describeAnomalyError", () => {
  it("explains a missing consent in words an Operator's manager can act on", () => {
    const body = JSON.stringify({
      code: "learning_consent_required",
      capability: "telemetry_baseline",
      reason: "No Admin of this company has turned it on.",
    });
    const v = describeAnomalyError(new Error(`Failed request: /ai/native/anomaly/machines/3 | 403 | ${body}`));
    expect(v.kind).toBe("consent_required");
    expect(v.text).toMatch(/an Admin can turn it on/i);
    expect(v.detail).toBe("No Admin of this company has turned it on.");
    expect(v.alert).toBe(false);
  });

  it("says a platform preview cannot run the learning step, without pointing at consent", () => {
    const body = JSON.stringify({
      code: "learning_not_from_preview",
      reason: "You are previewing TA from the platform workspace.",
    });
    const v = describeAnomalyError(new Error(`Failed request: /ai/native/anomaly/machines/3 | 403 | ${body}`));
    expect(v.kind).toBe("preview_not_allowed");
    expect(v.text).toMatch(/preview/i);
    expect(v.text).not.toMatch(/turn it on/i);
    expect(v.detail).toBe("You are previewing TA from the platform workspace.");
    expect(v.alert).toBe(false);
  });

  it("does not mistake a role refusal for a consent refusal", () => {
    const v = describeAnomalyError(new Error('Failed request: /x | 403 | {"detail":"You do not have permission"}'));
    expect(v.kind).toBe("error");
  });

  it("handles a consent refusal without a reason, a 404, and junk", () => {
    const noReason = describeAnomalyError(new Error('Failed request: /x | 403 | {"code":"learning_consent_required"}'));
    expect(noReason).toMatchObject({ kind: "consent_required", detail: null });
    expect(describeAnomalyError(new Error("Failed request: /x | 404 | {}")).text).toMatch(/not found/);
    expect(describeAnomalyError(new Error("Failed request: /x | 500 | <html>")).kind).toBe("error");
    expect(describeAnomalyError(new Error("Failed request: /x | 403 | null")).kind).toBe("error");
    expect(describeAnomalyError("Failed to fetch").kind).toBe("error");
    expect(describeAnomalyError(undefined).kind).toBe("error");
  });
});

describe("describeAnomaly", () => {
  it("says what history is missing, and scores nothing", () => {
    const v = describeAnomaly({
      status: "insufficient_history",
      score: null,
      needed: { fit_buckets_per_signal: 288, distinct_days: 3, calibration_buckets: 100, scorable_signals: 1 },
      have: { fit_buckets_per_signal: 288, distinct_days: 2, calibration_buckets: 0, scorable_signals: 0 },
    });
    expect(v.kind).toBe("insufficient_history");
    expect(v.text).toBe("Not enough history yet (have 2 of 3 days of telemetry).");
  });

  it("still says not enough history when the counts are missing", () => {
    expect(describeAnomaly({ status: "insufficient_history", score: null }).text).toBe("Not enough history yet.");
  });

  it("reports an unavailable evaluation with its reason", () => {
    const v = describeAnomaly({ status: "model_unavailable", score: null, reason: "pinned hash mismatch" });
    expect(v).toMatchObject({ kind: "unavailable", detail: "pinned hash mismatch" });
    expect(describeAnomaly({ status: "ok", score: null }).detail).toBeNull();
  });

  it("labels a score experimental unless the evaluation was adopted, and never as an alert", () => {
    const experimental = describeAnomaly({
      status: "ok",
      score: 0.995,
      simulated_source: true,
      deviating: [{ signal: "iot:bearing_temp_c" }],
      evaluation: { adopted: false, experimental: true, caveat: "c" },
    });
    expect(experimental.text).toMatch(/^Experimental:/);
    expect(experimental.text).toMatch(/simulator/);
    expect(experimental.detail).toBe("Furthest from normal: iot:bearing_temp_c");
    expect(experimental.alert).toBe(false);
    const adopted = describeAnomaly({
      status: "ok",
      score: 0.42,
      evaluation: { adopted: true, experimental: false, caveat: "c" },
    });
    expect(adopted.text).not.toMatch(/Experimental/);
    expect(adopted.detail).toBeNull();
    expect(adopted.alert).toBe(false);
  });

  it("describes a score as a rank against the machine's own recent hours, never as NN / 100", () => {
    const v = describeAnomaly({
      status: "ok",
      score: 0.7117,
      evaluation: { adopted: false, experimental: true, caveat: "c" },
    });
    expect(v.text).toMatch(/rarer than 71% of this machine's recent hours/);
    expect(v.text).not.toMatch(/\/ 100/);
    expect(v.text).toMatch(/not a probability/i);
  });

  it("cites the MEASURED false-alarm rate at the alarm level when the evaluation reports it", () => {
    const v = describeAnomaly({
      status: "ok",
      score: 0.99,
      deviating: [{ signal: "iot:vibration_mm_s" }],
      evaluation: {
        adopted: false,
        experimental: true,
        caveat: "c",
        alarm_score: 0.99,
        clean_false_alarm_rate: { estimate: 0.0155, lo: 0.0114, hi: 0.0205 },
      },
    });
    expect(v.detail).toContain("Furthest from normal: iot:vibration_mm_s");
    expect(v.detail).toMatch(/1\.6% of clean hours/);
    expect(v.detail).toMatch(/99%/);
    expect(v.detail).toMatch(/synthetic/);
  });
});
