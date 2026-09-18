import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * No £ on the cost-of-losses cards without the tenant's own unit value (ADR-0010).
 *
 * ai/cost.py priced every tenant's downtime at a fixed £12 a minute and every
 * scrapped unit at a fixed £25, and these cards money()'d the result, so a plant
 * that had never told AMP its margin was shown a precise-looking £ figure it never
 * supplied. The backend now measures losses in good units and sends every *_cost
 * as null until a unit value is set. These tests render the real cards against both
 * payloads: without a rate the currency must not appear anywhere on the card,
 * and with one the £ is the backend's figure, not a re-derived one.
 *
 * CostIntelCard also printed a literal "$" before every machine's figure, a
 * currency the product does not use at all.
 */

const apiGet = vi.fn();

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
}));

import CostSnapshot from "./CostSnapshot";
import CostIntelCard from "./CostIntelCard";
import { CURRENCY } from "../lib/money";

function row(over: Record<string, unknown>) {
  return {
    downtime_minutes: 40, rejected_units: 10, downtime_lost_units: 8, lost_units: 18,
    downtime_cost: null, scrap_cost: null, cost: null, ...over,
  };
}

function summary(priced: boolean) {
  const money = (n: number) => (priced ? n : null);
  return {
    has_data: true,
    days: 7,
    priced,
    unit_value_gbp: priced ? 12.5 : null,
    loss_cost: money(225),
    downtime_cost: money(100),
    scrap_cost: money(125),
    downtime_minutes: 40,
    rejected_units: 10,
    downtime_lost_units: 8,
    lost_units: 18,
    losses: [
      { key: "downtime", label: "Downtime", units: 8, cost: money(100), detail: "40 min ≈ 8 good units not made" },
      { key: "scrap", label: "Scrap", units: 10, cost: money(125), detail: "10 units scrapped" },
    ],
    biggest: "scrap",
    by_line: [
      { line: "SMT", ...row({ downtime_cost: money(60), scrap_cost: money(75), cost: money(135), lost_units: 11 }) },
      { line: "IC", ...row({ downtime_cost: money(40), scrap_cost: money(50), cost: money(90), lost_units: 7 }) },
    ],
    by_machine: [
      { machine_id: 1, name: "PRESS-01", ...row({ downtime_cost: money(100), scrap_cost: money(125), cost: money(225) }) },
    ],
    daily: [{ date: "2026-09-17", cost: money(225), lost_units: 18 }],
    recorded_total: 0,
    by_type: [],
  };
}

beforeEach(() => {
  apiGet.mockReset();
});

describe("CostSnapshot", () => {
  it("shows good units and no currency when the tenant has no unit value", async () => {
    apiGet.mockResolvedValue(summary(false));
    const { container } = render(<CostSnapshot />);
    await screen.findByText("18 units");
    expect(container.textContent).not.toContain(CURRENCY);
    expect(container.textContent).toContain("set a unit value to see money");
    expect(screen.getByText("PRESS-01", { exact: false })).toBeTruthy();
  });

  it("shows the backend's money figure once a unit value is set", async () => {
    apiGet.mockResolvedValue(summary(true));
    const { container } = render(<CostSnapshot />);
    await screen.findByText(`${CURRENCY}225`);
    expect(container.textContent).not.toContain("set a unit value");
  });

  it("says unknown, not zero, when downtime had no run time to convert", async () => {
    apiGet.mockResolvedValue({ ...summary(true), loss_cost: null, downtime_cost: null, lost_units: null, downtime_lost_units: null });
    render(<CostSnapshot />);
    await screen.findByText("downtime with no run time to convert");
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});

describe("CostIntelCard", () => {
  it("never prints a dollar, and no currency at all without a unit value", async () => {
    apiGet.mockImplementation((p: string) =>
      Promise.resolve(p === "/cost-summary" ? summary(false) : { verdict: "Losses up 12 good units", tone: "bad" }));
    const { container } = render(<CostIntelCard />);
    await waitFor(() => expect(container.textContent).toContain("PRESS-01"));
    expect(container.textContent).not.toContain("$");
    expect(container.textContent).not.toContain(CURRENCY);
    expect(container.textContent).toContain("18 units (downtime 8 units · scrap 10 units)");
  });

  it("prints the machine's money in the platform currency when priced", async () => {
    apiGet.mockImplementation((p: string) =>
      Promise.resolve(p === "/cost-summary" ? summary(true) : {}));
    const { container } = render(<CostIntelCard />);
    await waitFor(() => expect(container.textContent).toContain("PRESS-01"));
    expect(container.textContent).not.toContain("$");
    expect(container.textContent).toContain(`${CURRENCY}225 (downtime ${CURRENCY}100 · scrap ${CURRENCY}125)`);
  });
});
