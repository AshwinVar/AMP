import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiGet = vi.fn();
vi.mock("../lib/api", () => ({ apiGet: (...a: unknown[]) => apiGet(...a) }));

import RootCauseSection, { contributorSize } from "./RootCauseSection";
import { CURRENCY } from "../lib/money";

/**
 * The Root-Cause Explorer card (ADR-0025). The property that matters: a plant
 * whose stoppages are mostly unlogged must not read as explained, and a
 * correlated event must not look like a confirmed cause.
 */
function card(over: Record<string, unknown> = {}) {
  return {
    generated_at: "2026-09-20T08:00:00", days: 7, state: "PARTIAL DATA",
    headline: "1,200 units short of the plans that came due. AMP measured 2,480 units of lost capacity in the "
      + "window: 985 with a reason recorded (biggest: slow running, 450 units), and 1,495 with none — planned "
      + "time that was not running and carries no stoppage reason.",
    gap: { state: "OK", planned_units: 2500, actual_units: 1300, gap_units: 1200, gap_money: null },
    contributors: [
      { key: "performance", label: "Slow running", mechanism: "performance", units: 450, minutes: null,
        money: null, cause_label: "CAUSE CONFIRMED", basis: "the units the runtime could have produced",
        facts: [{ id: "f1", key: "rc.performance_units", label: "Units lost to slow running", value: 450,
                  unit: "units", provenance: "DERIVED METRIC", source: "production_records",
                  window: "last 7 days", detail: "" }] },
      { key: "downtime_unlogged", label: "Planned time not running, with no reason logged",
        mechanism: "availability", units: 1495, minutes: 1495, money: null,
        cause_label: "INSUFFICIENT EVIDENCE", basis: "planned minutes with no stoppage reason", facts: [] },
      { key: "stock.out", label: "1 item(s) out of stock", mechanism: "supply", units: null, minutes: null,
        money: null, cause_label: "CORRELATED EVENT", basis: "no measured link to the units not made",
        facts: [] },
    ],
    by_machine: [{ machine: "CNC-01", units: 575, quality_units: 150, performance_units: 200,
                   availability_minutes: 1200, logged_minutes: 225 }],
    measured_loss_units: 2480, attributed_units: 985, unattributed_units: 1495,
    measured_loss_money: null, attributed_money: null,
    unexplained_units: 215, attributed_share_of_gap: 82, currency: null,
    denominator_note: "The gap is measured against the plan; the losses are measured against the machines' own "
      + "ideal cycle. They are different denominators, so the share below is an indication of size, not an "
      + "accounting identity.",
    facts: [],
    ...over,
  };
}

describe("RootCauseSection", () => {
  beforeEach(() => apiGet.mockReset());

  it("reports the gap, what was measured, and what has no reason recorded", async () => {
    apiGet.mockResolvedValue(card());
    render(<RootCauseSection />);
    expect(await screen.findByText(/1,200 units short/)).toBeTruthy();
    expect(screen.getByText("1,200 units")).toBeTruthy();
    expect(screen.getByText("2,480 units")).toBeTruthy();
    expect(screen.getByText("985 units")).toBeTruthy();
    expect(screen.getAllByText("1,495 units").length).toBeGreaterThan(0);
  });

  it("labels a confirmed mechanism, an unexplained block and a correlated event differently", async () => {
    apiGet.mockResolvedValue(card());
    render(<RootCauseSection />);
    expect(await screen.findByText("CAUSE CONFIRMED")).toBeTruthy();
    expect(screen.getByText("INSUFFICIENT EVIDENCE")).toBeTruthy();
    expect(screen.getByText("CORRELATED EVENT")).toBeTruthy();
    // A correlated event claims no size.
    const stock = screen.getByText("1 item(s) out of stock").closest("li");
    expect(stock?.textContent).toContain("not measured");
  });

  it("states the part of the gap nothing explains, as unknown", async () => {
    apiGet.mockResolvedValue(card());
    render(<RootCauseSection />);
    expect(await screen.findByText(/215 units of the gap are not accounted for/)).toBeTruthy();
    expect(screen.getByText("UNKNOWN")).toBeTruthy();
  });

  it("says the gap and the losses are measured against different things", async () => {
    apiGet.mockResolvedValue(card());
    render(<RootCauseSection />);
    expect(await screen.findByText(/different denominators/)).toBeTruthy();
  });

  it("opens the evidence behind a contributor", async () => {
    apiGet.mockResolvedValue(card());
    render(<RootCauseSection />);
    fireEvent.click(await screen.findByRole("button", { name: /show evidence \(1\)/i }));
    expect(screen.getByText("Units lost to slow running")).toBeTruthy();
    expect(screen.getByText("DERIVED METRIC")).toBeTruthy();
  });

  it("says 'no plan set' rather than a zero gap", async () => {
    apiGet.mockResolvedValue(card({ gap: { state: "NOT CONFIGURED", planned_units: 0, actual_units: 0,
      gap_units: null, gap_money: null } }));
    render(<RootCauseSection />);
    expect(await screen.findByText("no plan set")).toBeTruthy();
  });

  it("names the worst machines", async () => {
    apiGet.mockResolvedValue(card());
    render(<RootCauseSection />);
    expect(await screen.findByText(/Worst machines: CNC-01 \(575 units\)/)).toBeTruthy();
  });
});

describe("contributorSize", () => {
  it("prefers money, then units, then minutes, and says when nothing was measured", () => {
    expect(contributorSize({ money: 5400, units: 450, minutes: null })).toBe(`${CURRENCY}${(5400).toLocaleString()}`);
    expect(contributorSize({ money: null, units: 450, minutes: 225 })).toBe("450 units");
    expect(contributorSize({ money: null, units: null, minutes: 225 })).toBe("225 min");
    expect(contributorSize({ money: null, units: null, minutes: null })).toBe("not measured");
  });
});
