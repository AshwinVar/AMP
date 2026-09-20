import { describeSweepRow } from "../lib/aiModels";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", () => ({
  apiGet: vi.fn(() => new Promise(() => {})),
  getUserRole: () => "Operator",
}));

import AINativeModelsPanel, { AIModelCard } from "./AIModelCard";
import { apiGet } from "../lib/api";
import type { HeadlineRow, ModelCard } from "../lib/aiModels";

/**
 * A model card (ADR-0020) says what the evaluation said: what the model was
 * trained and evaluated on, always, the baseline beside every number, the
 * reasons a model was not adopted, its known limitations and stress-test rows,
 * and NO numbers at all when the artifact failed its integrity check.
 */

function stressRow(over: Partial<HeadlineRow> = {}): HeadlineRow {
  return {
    metric: "Brier score",
    dataset: "stress test: shock_driven",
    unit: "score",
    higher_is_better: false,
    model: 0.0529,
    model_lo: null,
    model_hi: null,
    baseline_name: "base-rate Brier (always predict the prevalence)",
    baseline: 0.0524,
    baseline_lo: null,
    baseline_hi: null,
    difference: null,
    difference_lo: null,
    difference_hi: null,
    ...over,
  };
}

function card(over: Partial<ModelCard> = {}): ModelCard {
  return {
    name: "copilot_intent",
    title: "AMP-native copilot intent model",
    purpose: "Proposes which answer a question needs.",
    baseline: "The keyword router.",
    default_behaviour: "Not adopted: the keyword router stays in charge.",
    caveat: "Evaluated on synthetic machines only; not evidence of accuracy on real plants.",
    available: true,
    status: "experimental",
    adopted: false,
    reason: null,
    reasons: ["the pool gain of +3.1 points is under 5 points"],
    sha256_pinned: "69e825efacdf",
    sha256: "69e825efacdf",
    model_name: "copilot_intent",
    version: "v1",
    headline: [
      {
        metric: "Routing accuracy",
        dataset: "held-out questions (64)",
        unit: "percent",
        higher_is_better: true,
        model: 0.59375,
        model_lo: null,
        model_hi: null,
        baseline_name: "keyword router",
        baseline: 0.5625,
        baseline_lo: null,
        baseline_hi: null,
        difference: 0.03125,
        difference_lo: -0.03125,
        difference_hi: 0.09375,
      },
    ],
    ...over,
  };
}

describe("AIModelCard", () => {
  it("shows an unadopted model as experimental, beside its baseline, with its reasons", () => {
    render(<AIModelCard card={card()} />);
    expect(screen.getByText("Experimental · not adopted")).toBeTruthy();
    expect(screen.getByText("Trained and evaluated on AMP-authored questions only")).toBeTruthy();
    expect(screen.getByText("59.4%")).toBeTruthy();
    expect(screen.getByText("56.3%")).toBeTruthy();
    expect(screen.getByText("keyword router")).toBeTruthy();
    expect(screen.getByText(/No clear difference from the keyword router/)).toBeTruthy();
    expect(screen.getByText("the pool gain of +3.1 points is under 5 points")).toBeTruthy();
  });

  it("shows no numbers from an artifact that failed its integrity check", () => {
    render(
      <AIModelCard
        card={card({ available: false, status: "unavailable", adopted: null, reason: "pinned sha256 mismatch" })}
      />,
    );
    expect(screen.getByText("Unavailable")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("pinned sha256 mismatch");
    expect(screen.queryByText("59.4%")).toBeNull();
    expect(screen.queryByText("the pool gain of +3.1 points is under 5 points")).toBeNull();
    expect(screen.getByText("Trained and evaluated on AMP-authored questions only")).toBeTruthy();
  });

  it("never calls the anomaly check synthetic-only: it fits a baseline from the machine's own telemetry", () => {
    render(<AIModelCard card={card({ name: "telemetry_anomaly", title: "Telemetry anomaly check" })} />);
    expect(screen.queryByText(/^Trained and evaluated on synthetic data only$/)).toBeNull();
    expect(screen.getByText(/Evaluated on synthetic data only/)).toBeTruthy();
    expect(screen.getByText(/this machine's own telemetry/)).toBeTruthy();
  });

  it("shows an adopted model's known limitations and its stress-test rows, not only its best numbers", () => {
    render(
      <AIModelCard
        card={card({
          name: "failure_risk",
          adopted: true,
          status: "adopted",
          reasons: [],
          limitations: ["One raw input does about as well: reject_rate_drift alone ... includes zero."],
          misspecification_rows: [stressRow()],
        })}
      />,
    );
    expect(screen.getByText("Known limitations")).toBeTruthy();
    expect(screen.getByText(/One raw input does about as well/)).toBeTruthy();
    expect(screen.getByText(/Stress tests/)).toBeTruthy();
    expect(screen.getByText("stress test: shock_driven")).toBeTruthy();
    expect(screen.getByText("0.053")).toBeTruthy();
    expect(screen.getByText(/Worse than the base-rate Brier/)).toBeTruthy();
  });

  it("hides limitations and stress rows with every other number when the artifact failed its check", () => {
    render(
      <AIModelCard
        card={card({
          available: false,
          status: "unavailable",
          adopted: null,
          reason: "pinned sha256 mismatch",
          limitations: ["should not show"],
          misspecification_rows: [stressRow()],
        })}
      />,
    );
    expect(screen.queryByText("should not show")).toBeNull();
    expect(screen.queryByText("stress test: shock_driven")).toBeNull();
  });

  it("introduces the panel as models against their baselines, not against rules they would replace", async () => {
    vi.mocked(apiGet).mockResolvedValueOnce({ models: [card()], caveat: "Evaluated on synthetic machines only." });
    render(<AINativeModelsPanel />);
    const intro = await screen.findByText(/against its baseline on the same held-out data/);
    expect(intro.textContent).not.toMatch(/the rule it would replace/);
  });

  it("does not list not-adopted reasons for an adopted model", () => {
    render(<AIModelCard card={card({ adopted: true, status: "adopted", reasons: ["stale reason"] })} />);
    expect(screen.getByText("Adopted")).toBeTruthy();
    expect(screen.queryByText("stale reason")).toBeNull();
  });
});

describe("the fleet anomaly sweep (ADR-0032)", () => {
  it("shows every machine, and the reason for the ones with no score", () => {
    const rows = [
      { machine_id: 1, name: "CNC-01", line: "L1", score: 4.51, state: "MODEL NOT VALIDATED", reason: null },
      { machine_id: 2, name: "AOI-02", line: "L1", score: null, state: "INSUFFICIENT HISTORY",
        reason: "not enough history yet (have 2 of 7 distinct_days)" },
      { machine_id: 3, name: "OVEN-03", line: "L2", score: null, state: "NOT CONFIGURED",
        reason: "the artifact did not verify" },
    ];
    const said = rows.map(describeSweepRow);
    expect(said.map((r) => r.name)).toEqual(["CNC-01", "AOI-02", "OVEN-03"]);
    expect(said[0]).toMatchObject({ value: "4.51", muted: false });
    // A machine with no score is never a blank and never a zero.
    expect(said[1].value).toContain("not scored — not enough history");
    expect(said[1].muted).toBe(true);
    expect(said[2].value).toContain("the artifact did not verify");
    for (const r of said.slice(1)) {
      expect(r.value).not.toBe("0");
      expect(r.value).not.toBe("0.00");
    }
  });

  it("renders a score to two places, without inventing precision", () => {
    expect(describeSweepRow({ machine_id: 9, name: "X", line: "", score: 2, state: "MODEL NOT VALIDATED", reason: null }).value)
      .toBe("2.00");
  });
});
