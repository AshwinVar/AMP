import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import HealthExplanation, { type HealthExplanation as Data } from "./HealthExplanation";

/**
 * The health card must show the arithmetic, not just the verdict (ADR-0027).
 *
 * What is pinned here is what a reader is entitled to: every rule that cost
 * points appears with its points, the value it read and the threshold it read
 * against; the rules that passed are available; a capped score says it was
 * capped; and a machine that was never scored is never rendered as a healthy
 * one. The layout is not pinned — the claims are.
 */
const rule = (over: Partial<Data["deductions"][number]> = {}) => ({
  key: "downtime_high",
  label: "High accumulated downtime",
  points: 25,
  max_points: 25,
  reading: "145 min",
  measured: 145,
  unit: "min",
  threshold: "120 minutes or more in the risk window",
  reason: "high accumulated downtime",
  ...over,
});

const data = (over: Partial<Data> = {}): Data => ({
  health_score: 55,
  band: "Watch",
  band_rule: "80 or more is Healthy · 55 Watch · 35 At risk · below 35 Critical",
  start: 100,
  deductions: [rule(), rule({ key: "reject_high", label: "High reject rate", points: 20, reading: "9.1%", threshold: "8% or more of units rejected" })],
  clear: [rule({ key: "breakdown_now", label: "Currently in breakdown", points: 0, reading: "Running", threshold: "status is Breakdown" })],
  checks_run: 3,
  points_deducted: 45,
  points_before_cap: 45,
  capped: false,
  state: "OK",
  note: "Health starts at 100 and each rule below takes its points away. Every rule is a fixed threshold over recorded data, hand-weighted by AMP — not machine learning, and not a prediction of failure.",
  ...over,
});

describe("HealthExplanation", () => {
  it("shows the arithmetic that produced the score", () => {
    render(<HealthExplanation x={data()} />);
    expect(screen.getByText(/100 − 45 rule points =/)).toBeTruthy();
    expect(screen.getByText("55")).toBeTruthy();
  });

  it("gives every deduction its points, its reading and its threshold", () => {
    render(<HealthExplanation x={data()} />);
    expect(screen.getByText("−25")).toBeTruthy();
    expect(screen.getByText("−20")).toBeTruthy();
    expect(screen.getByText(/read 145 min · rule: 120 minutes or more/)).toBeTruthy();
    expect(screen.getByText(/read 9.1% · rule: 8% or more of units rejected/)).toBeTruthy();
  });

  it("never shows a points figure without the rule that produced it", () => {
    const { container } = render(<HealthExplanation x={data()} />);
    const items = Array.from(container.querySelectorAll("li"));
    expect(items.length).toBeGreaterThan(0);
    for (const li of items) {
      const text = li.textContent ?? "";
      if (/−\d+/.test(text)) expect(text).toMatch(/rule: .+/);
    }
  });

  it("keeps the rules that passed one click away", () => {
    render(<HealthExplanation x={data()} />);
    const toggle = screen.getByRole("button", { name: /1 rules that passed/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(screen.getByText("Currently in breakdown")).toBeTruthy();
    expect(screen.getByText(/read Running · rule: status is Breakdown/)).toBeTruthy();
  });

  it("says when the score hit its ceiling, and by how much", () => {
    render(<HealthExplanation x={data({ health_score: 0, band: "Critical", points_deducted: 100, points_before_cap: 137, capped: true })} />);
    expect(screen.getByText(/came to 137 points; the score stops at 100/)).toBeTruthy();
  });

  it("does not present an unscored machine as a healthy one", () => {
    const { container } = render(
      <HealthExplanation
        x={data({ health_score: null, band: null, deductions: [], clear: [], checks_run: 0, points_deducted: 0, state: "NOT MEASURED", note: "AMP has not scored this machine, so there is nothing to explain. A score of 100 here means no assessment ran, not that every check passed." })}
      />,
    );
    expect(screen.getByRole("status").textContent).toContain("NOT MEASURED");
    expect(container.textContent).not.toContain("Healthy");
    expect(container.textContent).not.toMatch(/\b100\/100\b/);
    expect(container.textContent).toContain("not that every check passed");
  });

  it("states plainly that all rules passed rather than showing an empty list", () => {
    render(<HealthExplanation x={data({ health_score: 100, band: "Healthy", deductions: [], points_deducted: 0, checks_run: 11 })} />);
    expect(screen.getByText(/All 11 health rules passed/)).toBeTruthy();
  });

  it("does not say every rule passed when nothing was recorded to read", () => {
    // PARTIAL DATA (ai/machine_health.py): the scorer had a row but no
    // downtime, production or breakdown in the risk window, so the six
    // history rules read nothing. The 100 is shown as arithmetic; "All 11
    // health rules passed" is not, and the backend's note says why.
    const note = "Nothing was recorded for this machine in the risk window — no downtime, no production, no breakdown — so 6 of the 11 rules read nothing and took no points off. The score is an absence, not a clean bill of health.";
    const { container } = render(
      <HealthExplanation
        x={data({ health_score: 100, band: "Healthy", deductions: [], points_deducted: 0, checks_run: 11,
                  state: "PARTIAL DATA", note, has_recorded_input: false, rules_unmeasured: 6,
                  clear: [rule({ key: "downtime_high", points: 0, reading: "not measured", measured: null })] })}
      />,
    );
    expect(screen.getByRole("status").textContent).toContain("PARTIAL DATA");
    expect(container.textContent).toContain("an absence, not a clean bill of health");
    expect(container.textContent).not.toMatch(/All 11 health rules passed/);
    // The arithmetic is still shown: it IS 100, for the rules that could read.
    expect(screen.getByText("100")).toBeTruthy();
  });

  it("carries the note denying that any of this is machine learning", () => {
    const { container } = render(<HealthExplanation x={data()} />);
    expect(container.textContent).toContain("not machine learning");
    expect(container.textContent).toContain("not a prediction of failure");
  });

  it("renders nothing at all when there is no explanation", () => {
    const { container } = render(<HealthExplanation x={null} />);
    expect(container.textContent).toBe("");
  });
});
