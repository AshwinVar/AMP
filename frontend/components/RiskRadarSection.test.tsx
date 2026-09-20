import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiGet = vi.fn();
vi.mock("../lib/api", () => ({ apiGet: (...a: unknown[]) => apiGet(...a) }));

import RiskRadarSection, { riskSize } from "./RiskRadarSection";
import { CURRENCY } from "../lib/money";
import { LIKELIHOOD } from "../lib/evidence";

/**
 * The Risk Radar card (ADR-0026). The property that matters: a likelihood word
 * never appears without the rule that earned it, and nothing on the card reads
 * as a prediction from a model.
 */
function radar(over: Record<string, unknown> = {}) {
  return {
    generated_at: "2026-09-20T08:00:00", state: "OK",
    headline: "4 thing(s) likely to become a problem; 5 on the radar in all.",
    measured_rate_per_day: 400,
    risks: [
      { key: "order.ORD-B-9", title: "ORD-B-9 is already past its date",
        detail: "400 units still to ship for Borealis Motors", likelihood: "LIKELY",
        rule: "the due date has passed and units are still unshipped", horizon: "now",
        module: "orders", view: "orders", impact_units: 400, impact_money: null, currency: null,
        facts: [{ id: "f1", key: "order.remaining", label: "ORD-B-9: units still to ship", value: 400,
                  unit: "units", provenance: "DERIVED METRIC", source: "customer_orders", window: "now",
                  detail: "" }] },
      { key: "machine.7", title: "CNC-01 is likely to stop", detail: "High accumulated downtime",
        likelihood: "LIKELY",
        rule: "the rule score is 100, at or above the 75 threshold; this is a hand-weighted rule, not a trained model",
        horizon: "now", module: "machines", view: "machinehealth", impact_units: null, impact_money: null,
        currency: null,
        facts: [{ id: "f2", key: "machine.score", label: "CNC-01: rule risk score", value: 100, unit: "/100",
                  provenance: "RULE-BASED ASSESSMENT", source: "predictive_engine", window: "now",
                  detail: "hand-weighted rule points, not machine learning" }] },
      { key: "stock.INV-001", title: "Steel coil is already out of stock", detail: "nothing on hand",
        likelihood: "WATCH", rule: "there is no stock on hand", horizon: "now", module: "inventory",
        view: "inventory", impact_units: null, impact_money: null, currency: null, facts: [] },
    ],
    note: "Every risk here is a rule over measured data, and each says which rule and which measurement "
      + "produced it. None of it is a prediction from a trained model.",
    ...over,
  };
}

describe("RiskRadarSection", () => {
  beforeEach(() => apiGet.mockReset());

  it("shows every risk with the rule that earned its likelihood", async () => {
    apiGet.mockResolvedValue(radar());
    render(<RiskRadarSection />);
    expect(await screen.findByText(/4 thing\(s\) likely/)).toBeTruthy();
    const rules = screen.getAllByText(/^Rule: /);
    expect(rules).toHaveLength(3);
    expect(screen.getAllByText("LIKELY")).toHaveLength(2);
    expect(screen.getByText("WATCH")).toBeTruthy();
  });

  it("never shows a likelihood without a rule", async () => {
    apiGet.mockResolvedValue(radar());
    const { container } = render(<RiskRadarSection />);
    await screen.findByText(/4 thing\(s\) likely/);
    for (const li of Array.from(container.querySelectorAll("li"))) {
      const word = LIKELIHOOD.find((w) => li.textContent?.includes(w));
      if (word) expect(li.textContent).toContain("Rule:");
    }
  });

  it("says the radar is rules over measured data, not a model", async () => {
    apiGet.mockResolvedValue(radar());
    render(<RiskRadarSection />);
    expect(await screen.findByText(/None of it is a prediction from a trained model/)).toBeTruthy();
    expect(screen.getByText(/measured output 400 units\/day/)).toBeTruthy();
  });

  it("shows the measured rule score as a rule-based assessment, not a probability", async () => {
    apiGet.mockResolvedValue(radar());
    render(<RiskRadarSection />);
    fireEvent.click((await screen.findAllByRole("button", { name: /show evidence \(1\)/i }))[1]);
    expect(screen.getByText("RULE-BASED ASSESSMENT")).toBeTruthy();
    expect(screen.getByText("100/100")).toBeTruthy();
    expect(screen.queryByText(/%\s*chance/i)).toBeNull();
  });

  it("sizes a risk only where there is a size", async () => {
    apiGet.mockResolvedValue(radar());
    render(<RiskRadarSection />);
    expect(await screen.findByText("400 units")).toBeTruthy();
    const stock = screen.getByText("Steel coil is already out of stock").closest("li");
    expect(stock?.textContent).not.toMatch(/\d+ units/);
  });

  it("shows an insufficient-history state rather than guessing", async () => {
    apiGet.mockResolvedValue(radar({ state: "INSUFFICIENT HISTORY", measured_rate_per_day: null,
      headline: "No production has been recorded this week, so AMP cannot say whether the orders on the book are reachable.",
      risks: [] }));
    render(<RiskRadarSection />);
    expect((await screen.findByRole("status")).textContent).toMatch(/INSUFFICIENT HISTORY/);
    expect(screen.getByText("Nothing on the radar right now.")).toBeTruthy();
  });
});

describe("riskSize", () => {
  it("prefers money, then units, and says nothing when there is no size", () => {
    expect(riskSize({ impact_money: 4800, impact_units: 400 })).toBe(`${CURRENCY}${(4800).toLocaleString()}`);
    expect(riskSize({ impact_money: null, impact_units: 400 })).toBe("400 units");
    expect(riskSize({ impact_money: null, impact_units: null })).toBeNull();
  });
});
