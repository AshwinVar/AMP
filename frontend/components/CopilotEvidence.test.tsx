import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import CopilotEvidence from "./CopilotEvidence";
import type { Fact } from "../lib/evidence";
import { CURRENCY } from "../lib/money";

/**
 * The evidence under a Copilot answer (ADR-0022): the data state is always shown
 * when the answer is not complete, the facts are one tap away with their
 * provenance, and a rejected model wording is explained without quoting it.
 */
function fact(over: Partial<Fact> = {}): Fact {
  return {
    id: "F1", key: "downtime.minutes", label: "Minutes lost", value: 285, unit: "min",
    provenance: "MEASURED FACT", source: "downtime_logs", window: "last 7 days", detail: "", ...over,
  };
}

describe("CopilotEvidence", () => {
  it("renders nothing for an OK answer with no facts or tools", () => {
    const { container } = render(<CopilotEvidence state="OK" />);
    expect(container.innerHTML).toBe("");
  });

  it("always shows a not-configured state, before any tap", () => {
    render(<CopilotEvidence state="NOT CONFIGURED" facts={[fact()]} />);
    expect(screen.getByRole("status").textContent).toMatch(/NOT CONFIGURED/);
    expect(screen.queryByText("Minutes lost")).toBeNull();
  });

  it("shows each fact with its value and provenance when opened", () => {
    render(
      <CopilotEvidence
        state="OK"
        facts={[fact(), fact({ id: "F2", key: "machine.health", label: "Health score", value: 55, unit: "/100",
          provenance: "RULE-BASED ASSESSMENT" })]}
        tools={[{ tool: "get_downtime", state: "OK", summary: "" }]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /show evidence \(2 facts\)/i }));
    expect(screen.getByText("285 min")).toBeTruthy();
    expect(screen.getByText("MEASURED FACT")).toBeTruthy();
    expect(screen.getByText("55/100")).toBeTruthy();
    const rule = screen.getByText("RULE-BASED ASSESSMENT");
    expect(rule.getAttribute("title")).toMatch(/not machine learning/i);
    expect(screen.getByText(/Answered by AMP tools: get_downtime \(OK\)/)).toBeTruthy();
  });

  it("says a missing money figure is unknown", () => {
    render(<CopilotEvidence facts={[fact({ key: "losses.cost", label: "Cost of losses in money", value: null,
      unit: CURRENCY, provenance: "UNKNOWN" })]} />);
    fireEvent.click(screen.getByRole("button", { name: /show evidence/i }));
    expect(screen.getByText("unknown")).toBeTruthy();
  });

  it("explains a rejected model wording by its counts", () => {
    render(<CopilotEvidence facts={[fact()]} engine="rules"
      grounding={{ passed: false, numbers_checked: 2, reasons: ["2 number(s) not in the evidence"] }} />);
    fireEvent.click(screen.getByRole("button", { name: /show evidence/i }));
    expect(screen.getByText(/wording was not shown \(2 number\(s\) not in the evidence\)/)).toBeTruthy();
  });

  it("says a model's wording was checked when it was shown", () => {
    render(<CopilotEvidence facts={[fact()]} engine="llm" grounding={{ passed: true, numbers_checked: 1, reasons: [] }} />);
    fireEvent.click(screen.getByRole("button", { name: /show evidence/i }));
    expect(screen.getByText(/every figure and name was checked/)).toBeTruthy();
  });
});
