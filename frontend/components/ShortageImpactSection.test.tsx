import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import ShortageImpactSection, { shortageSize } from "./ShortageImpactSection";

/**
 * The shortage card (ADR-0030).
 *
 * The one thing it must never do is blur its two cases: an item with a recipe
 * gets arithmetic, an item without one gets no number at all and is still
 * listed. So the tests are about which items carry figures, not about layout.
 */
vi.mock("../lib/api", () => ({ apiGet: vi.fn() }));
const { apiGet } = await import("../lib/api");

const impact = (over: Record<string, unknown> = {}) => ({
  state: "PARTIAL DATA",
  headline:
    "170 units of production cannot be made from the stock on hand. The biggest is Steel bar: 170 units across 4 open orders.",
  units_at_risk: 170,
  money_at_risk: 2040,
  priced: true,
  shortages: [
    {
      item_code: "RM-STEEL",
      item_name: "Steel bar",
      unit: "kg",
      on_hand: 90,
      required_units: 430,
      shortfall_units: 340,
      units_at_risk: 170,
      money_at_risk: 2040,
      currency: "£",
      orders_affected: 4,
      suggested_order_units: 340,
      basis: "Stock on hand is given to the open orders in due-date order, earliest first.",
      orders: [
        { work_order_no: "WO-EARLY", part_number: "SHAFT-001", outstanding: 100, per_unit: 2,
          component_unit: "kg", units_at_risk: 55, planned_end: "2026-09-22T09:00:00" },
        { work_order_no: "WO-MID", part_number: "SHAFT-001", outstanding: 50, per_unit: 2,
          component_unit: "kg", units_at_risk: 50, planned_end: "2026-09-25T09:00:00" },
      ],
    },
  ],
  unlinked: [
    { item_code: "RM-PAINT", item_name: "Paint", on_hand: 0, unit: "L",
      why: "AMP has no bill of materials linking this item to anything in production, so it cannot say what running out would stop." },
  ],
  note: "Stock on hand is given to the open orders in due-date order, earliest first.",
  ...over,
});

describe("ShortageImpactSection", () => {
  beforeEach(() => {
    vi.mocked(apiGet).mockReset();
    vi.mocked(apiGet).mockResolvedValue(impact());
  });

  it("sizes a shortage that has a recipe", async () => {
    render(<ShortageImpactSection />);
    expect(await screen.findByText("Steel bar")).toBeTruthy();
    expect(screen.getByText(/90 kg on hand · the open orders need 430 kg · short by 340 kg/)).toBeTruthy();
    expect(screen.getByText("4 open orders")).toBeTruthy();
  });

  it("gives the item with no recipe no figure at all, but still lists it", async () => {
    const { container } = render(<ShortageImpactSection />);
    await screen.findByText("Paint");
    expect(screen.getByText(/Short, but AMP cannot say what it would stop \(1\)/)).toBeTruthy();
    expect(screen.getByText(/no bill of materials linking this item/)).toBeTruthy();
    // The unlinked block carries the stock on hand, and no units-at-risk figure.
    const block = container.textContent ?? "";
    expect(block).toContain("0 L on hand");
  });

  it("keeps the allocation rule with the allocation", async () => {
    render(<ShortageImpactSection />);
    const toggle = await screen.findByRole("button", { name: /Show the orders \(2\)/ });
    fireEvent.click(toggle);
    expect(screen.getByText("WO-EARLY")).toBeTruthy();
    expect(screen.getByText(/55 cannot be made/)).toBeTruthy();
    expect(screen.getByText(/due-date order, earliest first/)).toBeTruthy();
  });

  it("states the risk in units when no unit value is set", async () => {
    vi.mocked(apiGet).mockResolvedValue(impact({
      priced: false,
      money_at_risk: null,
      shortages: [{ ...impact().shortages[0], money_at_risk: null, currency: null }],
    }));
    render(<ShortageImpactSection />);
    expect(await screen.findByText("170 units")).toBeTruthy();
    expect(screen.getByText(/given in units of production, not money/)).toBeTruthy();
  });

  it("formats a size as money only when there is money", () => {
    expect(shortageSize({ money_at_risk: 2040, units_at_risk: 170 })).toContain("2,040");
    expect(shortageSize({ money_at_risk: null, units_at_risk: 170 })).toBe("170 units");
  });

  it("says plainly when nothing is short", async () => {
    vi.mocked(apiGet).mockResolvedValue(impact({
      state: "OK", headline: "Nothing is at or below its reorder level.",
      units_at_risk: 0, money_at_risk: null, shortages: [], unlinked: [],
    }));
    render(<ShortageImpactSection />);
    // The headline and the empty state say the same sentence, which is the
    // point: the card does not go blank, it says the thing.
    await waitFor(() =>
      expect(screen.getAllByText("Nothing is at or below its reorder level.").length).toBe(2));
  });

  it("renders nothing rather than an empty shell when the load fails", async () => {
    vi.mocked(apiGet).mockRejectedValue(new Error("boom"));
    const { container } = render(<ShortageImpactSection />);
    await waitFor(() => expect(container.textContent).toBe(""));
  });
});
