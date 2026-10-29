import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The one money gate (lib/money-visibility): a non-Admin sees NO rupee figure on
 * a money-bearing card, an Admin sees them, and the card renders cleanly either
 * way — from the SAME priced payload, so this proves the front-end gate, not just
 * the backend omitting the amount.
 */

const apiGet = vi.fn();
vi.mock("../lib/api", () => ({ apiGet: (p: string) => apiGet(p) }));

import { CanSeeMoneyProvider, useCanSeeMoney } from "../lib/money-visibility";
import { CURRENCY } from "../lib/money";
import CostSnapshot from "./CostSnapshot";

// An Admin's payload: money present in every field (the backend omits these for
// a non-Admin, but the gate must hold even when an amount IS present).
function pricedSummary() {
  const lossRow = (over: Record<string, unknown>) => ({
    downtime_minutes: 40, rejected_units: 10, downtime_lost_units: 8, lost_units: 18,
    downtime_cost: 100, scrap_cost: 125, cost: 225, ...over,
  });
  return {
    has_data: true, priced: true, unit_value_gbp: 12.5,
    loss_cost: 225, lost_units: 18,
    losses: [
      { key: "downtime", label: "Downtime", units: 8, cost: 100, detail: "40 min" },
      { key: "scrap", label: "Scrap", units: 10, cost: 125, detail: "10 units scrapped" },
    ],
    biggest: "scrap",
    by_line: [{ line: "SMT", ...lossRow({}) }],
    by_machine: [{ machine_id: 1, name: "PRESS-01", ...lossRow({}) }],
    daily: [{ date: "2026-09-17", cost: 225, lost_units: 18 }],
    recorded_total: 5000,
    by_type: [{ type: "Material", amount: 5000 }],
  };
}

describe("useCanSeeMoney / CanSeeMoneyProvider", () => {
  function Probe() {
    return <span data-testid="probe">{useCanSeeMoney() ? "yes" : "no"}</span>;
  }

  it("defaults to true with no provider, so a standalone card shows money", () => {
    render(<Probe />);
    expect(screen.getByTestId("probe").textContent).toBe("yes");
  });

  it("returns exactly what the nearest provider was given", () => {
    const { rerender } = render(
      <CanSeeMoneyProvider value={false}>
        <Probe />
      </CanSeeMoneyProvider>,
    );
    expect(screen.getByTestId("probe").textContent).toBe("no");
    rerender(
      <CanSeeMoneyProvider value={true}>
        <Probe />
      </CanSeeMoneyProvider>,
    );
    expect(screen.getByTestId("probe").textContent).toBe("yes");
  });
});

describe("a money-bearing card read through the gate", () => {
  beforeEach(() => apiGet.mockReset());

  it("shows an Admin the rupee figures", async () => {
    apiGet.mockResolvedValue(pricedSummary());
    const { container } = render(
      <CanSeeMoneyProvider value={true}>
        <CostSnapshot />
      </CanSeeMoneyProvider>,
    );
    await waitFor(() => expect(container.textContent).toContain(`${CURRENCY}225`));
  });

  it("shows a non-Admin good units and NO currency, from the same priced payload", async () => {
    apiGet.mockResolvedValue(pricedSummary());
    const { container } = render(
      <CanSeeMoneyProvider value={false}>
        <CostSnapshot />
      </CanSeeMoneyProvider>,
    );
    // The card still renders — the loss falls back to good units, not an empty hole.
    await waitFor(() => expect(container.textContent).toContain("PRESS-01"));
    expect(container.textContent).toContain("units");
    expect(container.textContent).not.toContain(CURRENCY);
  });
});
