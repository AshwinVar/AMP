import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiGet = vi.fn();
vi.mock("../lib/api", () => ({ apiGet: (...a: unknown[]) => apiGet(...a) }));

import CommandCentreSection, { impactLabel } from "./CommandCentreSection";
import { CURRENCY } from "../lib/money";

/**
 * The Command Centre card (ADR-0024). What the owner must be able to read without
 * asking anyone: where the plant is, what is wrong and what it cost, why, and
 * what to do — with the honest states visible rather than rounded away.
 */
function card(over: Record<string, unknown> = {}) {
  return {
    generated_at: "2026-09-20T08:00:00", days: 7, state: "OK",
    headline: "Plant OEE 53%; 52% of the plan due was made; 1 machine down right now (CNC-01). "
      + "Biggest measured loss: PLAN-B1 on CNC-01 is behind (600 good units).",
    position: {
      state: "OK", oee: 53, coverage_phrase: "",
      machines: { total: 3, running: 1, down: 1, down_names: ["CNC-01"], maintenance: 1, idle: 0 },
      output: { good: 2800, total: 3050, good_rate: 92, runs: 11, days: 7 },
      plan: { state: "OK", planned_units: 2500, actual_units: 1300, attainment_rate: 52, behind: 2, missed: 1 },
      facts: [],
    },
    problems: [
      {
        key: "machines.down", title: "1 machine down right now", detail: "CNC-01", module: "machines",
        view: "machines", impact_units: null, impact_money: null, currency: null,
        rank_basis: "stopped now", state: "NOT MEASURED", why: [],
        facts: [{ id: "machines.down_now", key: "machines.down_now", label: "Machines down", value: 1,
                  unit: "machines", provenance: "MEASURED FACT", source: "machines", window: "now", detail: "CNC-01" }],
      },
      {
        key: "plan.PLAN-B1", title: "PLAN-B1 on CNC-01 is behind", detail: "400 of 1,000 planned units",
        module: "planning", view: "planning", impact_units: 600, impact_money: null, currency: null,
        rank_basis: "measured loss", state: "OK",
        why: [{ label: "LIKELY CONTRIBUTOR", text: "CNC-01 lost 225 minutes to downtime in the same window",
                facts: [] }],
        facts: [{ id: "plan.shortfall", key: "plan.shortfall", label: "PLAN-B1: units short", value: 600,
                  unit: "units", provenance: "DERIVED METRIC", source: "production_plans", window: "last 7 days", detail: "" }],
      },
    ],
    overlap_note: "Each problem is sized on its own. Two problems can describe the same lost output, so these figures are not a total.",
    cost: { state: "NOT CONFIGURED", priced: false, loss_cost: null, lost_units: 1674, downtime_minutes: 285,
            rejected_units: 250, facts: [] },
    actions: [
      { key: "agent_action.1", title: "Raise a critical maintenance task on CNC-01",
        detail: "proposed by the maintenance agent", who: "An Admin or Supervisor approves it",
        module: "agentactivity", view: "agentactivity", state: "awaiting approval" },
    ],
    ...over,
  };
}

describe("CommandCentreSection", () => {
  beforeEach(() => apiGet.mockReset());

  it("shows the headline, the position and the machine that is down", async () => {
    apiGet.mockResolvedValue(card());
    render(<CommandCentreSection />);
    expect(await screen.findByText(/Biggest measured loss/)).toBeTruthy();
    expect(screen.getByText("53%")).toBeTruthy();
    expect(screen.getByText("1/3 running")).toBeTruthy();
    expect(screen.getByText("CNC-01 down")).toBeTruthy();
  });

  it("shows a loss in good units, and says no money figure can be shown, when no unit value is set", async () => {
    apiGet.mockResolvedValue(card());
    render(<CommandCentreSection />);
    expect(await screen.findByText("1,674 units")).toBeTruthy();
    expect(screen.getByText(new RegExp(`no unit value set, so no ${CURRENCY} figure`))).toBeTruthy();
    expect(screen.queryByText(new RegExp(`\\${CURRENCY}\\d`))).toBeNull();
  });

  it("ranks the live stoppage first and labels why each problem sits where it does", async () => {
    apiGet.mockResolvedValue(card());
    render(<CommandCentreSection />);
    await screen.findByText("1 machine down right now");
    // The problem TITLES in document order (the headline mentions both, so match whole strings).
    const titles = screen
      .getAllByText((_c, node) => node?.tagName === "P"
        && ["1 machine down right now", "PLAN-B1 on CNC-01 is behind"].includes(node.textContent ?? ""))
      .map((n) => n.textContent);
    expect(titles[0]).toBe("1 machine down right now");
    expect(screen.getByText("stopped now")).toBeTruthy();
    expect(screen.getByText("not measured")).toBeTruthy();
    expect(screen.getByText("600 good units")).toBeTruthy();
  });

  it("names a contributor with its label, and never as a confirmed cause", async () => {
    apiGet.mockResolvedValue(card());
    render(<CommandCentreSection />);
    expect(await screen.findByText("LIKELY CONTRIBUTOR")).toBeTruthy();
    expect(screen.getByText(/lost 225 minutes to downtime/)).toBeTruthy();
    expect(screen.queryByText("CAUSE CONFIRMED")).toBeNull();
  });

  it("says the figures are not a total, because problems can overlap", async () => {
    apiGet.mockResolvedValue(card());
    render(<CommandCentreSection />);
    expect(await screen.findByText(/not a total/)).toBeTruthy();
  });

  it("opens the evidence behind a problem on request", async () => {
    apiGet.mockResolvedValue(card());
    render(<CommandCentreSection />);
    const buttons = await screen.findAllByRole("button", { name: /show evidence \(1\)/i });
    expect(screen.queryByText("DERIVED METRIC")).toBeNull();
    fireEvent.click(buttons[1]);   // the plan problem
    expect(screen.getByText("PLAN-B1: units short")).toBeTruthy();
    expect(screen.getByText("DERIVED METRIC")).toBeTruthy();
  });

  it("says who decides the action waiting, not that AMP will", async () => {
    apiGet.mockResolvedValue(card());
    render(<CommandCentreSection />);
    expect(await screen.findByText("Raise a critical maintenance task on CNC-01")).toBeTruthy();
    expect(screen.getByText("An Admin or Supervisor approves it")).toBeTruthy();
    expect(screen.getByText("awaiting approval")).toBeTruthy();
  });

  it("shows a partial-data state instead of a bare figure", async () => {
    apiGet.mockResolvedValue(card({ state: "PARTIAL DATA",
      position: { ...card().position, coverage_phrase: "from 2 of 3 machines" } }));
    render(<CommandCentreSection />);
    expect((await screen.findByRole("status")).textContent).toMatch(/PARTIAL DATA/);
    expect(screen.getByText("from 2 of 3 machines")).toBeTruthy();
  });

  it("says 'no plan set' rather than 0% when there is no plan", async () => {
    apiGet.mockResolvedValue(card({ position: { ...card().position,
      plan: { state: "NOT CONFIGURED", planned_units: 0, actual_units: 0, attainment_rate: null, behind: 0, missed: 0 } } }));
    render(<CommandCentreSection />);
    expect(await screen.findByText("no plan set")).toBeTruthy();
    expect(screen.queryByText("0%")).toBeNull();
  });

});

describe("impactLabel", () => {
  it("prefers money, falls back to units, and says when a loss is not measured", () => {
    expect(impactLabel({ impact_money: 7200, impact_units: 600 })).toBe(`${CURRENCY}${(7200).toLocaleString()}`);
    expect(impactLabel({ impact_money: null, impact_units: 600 })).toBe("600 good units");
    expect(impactLabel({ impact_money: null, impact_units: null })).toBe("not measured");
  });
});
