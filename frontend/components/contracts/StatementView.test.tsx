import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import StatementView from "./StatementView";
import { content } from "./testFixtures";

/**
 * A statement as both parties read it (ADR-0020).
 *
 * The properties: the tiles are the statement's own totals and add up to the
 * covered time; unmeasured time says "No data" and is never drawn as uptime or
 * downtime; the credit is the server's decimal text to the paisa; and a period
 * that cannot be evaluated shows NO credit rather than a credit of zero.
 */

function tileSeconds(bucket: string) {
  return Number(screen.getAllByTestId(`tile-${bucket}`)[0].getAttribute("data-seconds"));
}

describe("StatementView", () => {
  it("draws tiles that sum to the covered seconds", () => {
    const c = content();
    render(<StatementView content={c} />);
    const sum = ["AVAILABLE", "OEM", "FACTORY", "DISPUTED", "UNMEASURED"]
      .map(tileSeconds).reduce((a, b) => a + b, 0);
    expect(sum).toBe(c.totals.covered_seconds);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("calls unmeasured time No data, with its exact duration", () => {
    render(<StatementView content={content()} />);
    expect(screen.getAllByTestId("tile-UNMEASURED")[0].textContent).toBe("No data: 8 h 46 min 40 s");
    const row = screen.getAllByText("No data").find((el) => el.tagName === "TD");
    expect(row?.closest("tr")?.textContent).toContain("No trusted telemetry received");
  });

  it("warns, and does not hide it, when the buckets do not add up", () => {
    const c = content();
    render(<StatementView content={{ ...c, totals: { ...c.totals, unmeasured_seconds: 0 } }} />);
    expect(screen.getByRole("alert").textContent).toMatch(/do not add up/);
  });

  it("shows the credit exactly, in rupees with Indian grouping", () => {
    render(<StatementView content={content()} />);
    expect(screen.getByTestId("credit-amount").textContent).toBe("₹61,728.35");
    expect(screen.getByTestId("sla-state").textContent).toBe("SLA breached");
    expect(screen.getByTestId("availability").textContent).toBe("96.27%");
    expect(screen.getByText(/never moves money|does not invoice, charge or move money/)).toBeTruthy();
  });

  it("shows no credit at all for a period that cannot be evaluated", () => {
    const c = content();
    render(<StatementView content={{
      ...c,
      sla: { ...c.sla, state: "not_evaluable", availability_pct: null,
             not_evaluable_reason: "insufficient measurement" },
      credit: { ...c.credit, amount: null, credit_pct: null, tier_below_pct: null },
    }} />);
    expect(screen.getByTestId("sla-state").textContent).toBe("Not evaluable");
    expect(screen.getByText(/insufficient measurement/)).toBeTruthy();
    expect(screen.queryByTestId("credit-amount")).toBeNull();
    expect(screen.getByTestId("credit").textContent).toBe("No credit is computed for this period.");
    expect(screen.getByTestId("credit").textContent).not.toMatch(/0\.00/);
  });

  it("shows a range, not an amount, while disputes are pending", () => {
    const c = content();
    render(<StatementView content={{
      ...c,
      sla: { ...c.sla, state: "pending_disputes", availability_pct: null,
             availability_if_disputes_oem: "96.13", availability_if_disputes_factory: "96.26",
             availability_if_disputes_available: "96.27" },
      credit: { ...c.credit, amount: null, credit_pct: null, range_min: "61728.35",
                range_max: "61728.35" },
    }} />);
    const credit = within(screen.getByTestId("credit"));
    expect(credit.getByText(/between ₹61,728\.35 and ₹61,728\.35/)).toBeTruthy();
    expect(screen.queryByTestId("credit-amount")).toBeNull();
    expect(screen.getByText(/all of it is the manufacturer/).textContent).toContain("96.13%");
  });

  it("labels a preview as not a statement", () => {
    render(<StatementView content={content()} preview />);
    expect(screen.getByText(/cannot be accepted/)).toBeTruthy();
  });
});
