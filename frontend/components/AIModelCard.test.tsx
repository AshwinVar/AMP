import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", () => ({
  apiGet: vi.fn(() => new Promise(() => {})),
  getUserRole: () => "Operator",
}));

import { AIModelCard } from "./AIModelCard";
import type { ModelCard } from "../lib/aiModels";

/**
 * A model card (ADR-0020) says what the evaluation said: the synthetic-only
 * label always, the baseline beside every number, the reasons a model was not
 * adopted, and NO numbers at all when the artifact failed its integrity check.
 */

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
    expect(screen.getByText("Trained and evaluated on synthetic data only")).toBeTruthy();
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
    expect(screen.getByText("Trained and evaluated on synthetic data only")).toBeTruthy();
  });

  it("does not list not-adopted reasons for an adopted model", () => {
    render(<AIModelCard card={card({ adopted: true, status: "adopted", reasons: ["stale reason"] })} />);
    expect(screen.getByText("Adopted")).toBeTruthy();
    expect(screen.queryByText("stale reason")).toBeNull();
  });
});
