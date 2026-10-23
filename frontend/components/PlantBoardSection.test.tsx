import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PlantBoardDay, PlantBoardMonth } from "../lib/plant-board";

/**
 * The board's job is to be POINTED AT in a room with a customer, so the things
 * worth testing are the ones that would embarrass it there:
 *
 *   * power and packing must read as "not measured", never as a zero. The
 *     customer explicitly has no source for either, and a flat line at zero
 *     looks exactly like a chart with data.
 *   * a machine with no part specification must still show its parts. The shot
 *     count is real; only the conversions are missing.
 *   * money must not appear at all until something is priced. "₹0" for an
 *     unpriced part is a figure nobody can act on and one somebody will query.
 */

const apiGet = vi.hoisted(() => vi.fn());
vi.mock("../lib/api", () => ({
  apiGet,
  errorDetail: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}));
// The part master does its own fetching and is tested on its own; here it would
// only add noise and a second pair of requests.
vi.mock("./PartMasterCard", () => ({ default: () => null }));

import PlantBoardSection from "./PlantBoardSection";

const UNAVAILABLE = {
  available: false as const,
  reason: "No energy meter is installed on any machine.",
  fix: "A 3-phase Modbus meter per machine.",
  points: [] as [],
};

function hours(parts: number, ideal: number, status: "ok" | "low" | "unrated") {
  return Array.from({ length: 24 }, (_, h) => ({
    hour: h,
    parts: h === 9 ? parts : 0,
    shots: h === 9 ? parts : 0,
    ideal,
    status: h === 9 ? status : status === "unrated" ? ("unrated" as const) : ("low" as const),
  }));
}

function day(over: Partial<PlantBoardDay> = {}): PlantBoardDay {
  return {
    date: "2026-10-07",
    acceptable_fraction: 0.8,
    machines: [{ id: 1, name: "IMM-01", status: "Running" }],
    production: [{
      machine_id: 1, machine: "IMM-01", part: "Ele clip", part_code: "ELE-CLIP",
      tool: "MLD-ELE", ideal_per_hour: 14400, average_per_hour: 600, total: 14400,
      counts_are_parts: true, shots: 14400, hours_known: true, hourly_total: 14400,
      points: hours(14400, 14400, "ok"),
    }],
    rm_status: [{
      machine_id: 1, machine: "IMM-01", material: "PP H 110", kg_total: 4.752,
      points: Array.from({ length: 24 }, (_, h) => ({ hour: h, kg: h === 9 ? 4.752 : 0 })),
    }],
    shift_rate: [{
      machine_id: 1, machine: "IMM-01", priced: true,
    points: [
        { shift: 1, from_hour: 0, to_hour: 8, parts: 0, revenue: 0, rate_per_hour: 0 },
        { shift: 2, from_hour: 8, to_hour: 16, parts: 14400, revenue: 1296, rate_per_hour: 162 },
        { shift: 3, from_hour: 16, to_hour: 24, parts: 0, revenue: 0, rate_per_hour: 0 },
      ],
    }],
    power: { ...UNAVAILABLE },
    packing: { ...UNAVAILABLE, reason: "Nothing records packed quantities.",
               fix: "A packing entry screen or a weighing scale." },
    ...over,
  };
}

function month(over: Partial<PlantBoardMonth> = {}): PlantBoardMonth {
  return {
    daily: [],
    year: 2026, month: 10,
    itemwise_production: [{ part: "Ele clip", total: 432000, good: 428000 }],
    rm_consumption: [{ material: "PP H 110", kg: 142.56 }],
    rm_added: [],
    rm_added_daily: [],
    rm_balance: [{ material: "PP H 110", consumed_kg: 142.56, added_kg: null,
                   added_qty: null, added_unit: null }],
    shift_rate_by_machine: [{ machine: "IMM-01", revenue: 1400000, rate_per_hour: 1882 }],
    total_rate_per_hour: 1882,
    total_revenue: 1400000,
    power: { ...UNAVAILABLE },
    packing: { ...UNAVAILABLE },
    ...over,
  };
}

function serve(d: PlantBoardDay, m: PlantBoardMonth) {
  apiGet.mockImplementation((path: string) =>
    Promise.resolve(path.includes("/month") ? m : d));
}

beforeEach(() => {
  apiGet.mockReset();
});

describe("power and packing", () => {
  /**
   * THESE CARDS WERE REMOVED ON PURPOSE, at the customer's request on
   * 2026-10-12. They previously rendered "Not measured" with the gap and the
   * fix named, which was the right behaviour while they were on the board --
   * a flat line at zero looks exactly like a chart with data.
   *
   * This test is kept, inverted, so that re-adding them is a deliberate act
   * rather than something that drifts back in. The backend still returns both
   * series with `available: false`; the board simply does not draw them, and
   * a card that only ever says "no source" is one more thing to explain in a
   * room with a customer.
   */
  it("are not on the board, and not as a zero either", async () => {
    serve(day(), month());
    render(<PlantBoardSection isAdmin />);
    await screen.findByText("IMM-01");

    expect(screen.queryByText("Power consumption")).toBeNull();
    expect(screen.queryByText("Packing")).toBeNull();
    // The point of removing them is that nothing is claimed, so no zero may
    // appear in their place either.
    expect(screen.queryByText("No energy meter is installed on any machine.")).toBeNull();
    expect(screen.queryByText("Nothing records packed quantities.")).toBeNull();
  });
});

describe("a machine nobody has specified", () => {
  it("still reports the parts it made, and asks for the mould", async () => {
    serve(
      day({
        production: [{
          machine_id: 1, machine: "IMM-01", part: null, part_code: null, tool: null,
          ideal_per_hour: 0, average_per_hour: 208.3, total: 5000,
          counts_are_parts: true, shots: 14400, hours_known: true, hourly_total: 5000,
          points: hours(5000, 0, "unrated"),
        }],
        rm_status: [{ machine_id: 1, machine: "IMM-01", material: null, kg_total: 0,
    points: [] }],
        shift_rate: [{ machine_id: 1, machine: "IMM-01", priced: false, points: [] }],
      }),
      month({ itemwise_production: [], rm_consumption: [], shift_rate_by_machine: [],
              total_revenue: 0, total_rate_per_hour: 0 }),
    );
    render(<PlantBoardSection isAdmin />);

    // The count is real and is shown.
    await waitFor(() => expect(screen.getAllByText("5,000").length).toBeGreaterThan(0));
    // The target is absent rather than zero.
    expect(screen.getAllByText("not set").length).toBeGreaterThan(0);
    // And it says what to do about it.
    expect(screen.getByText(/no mould assigned/i)).toBeTruthy();
  });

  it("shows no money at all rather than a zero", async () => {
    serve(
      day({
        production: [{
          machine_id: 1, machine: "IMM-01", part: null, part_code: null, tool: null,
          ideal_per_hour: 0, average_per_hour: 0, total: 5000,
          counts_are_parts: true, shots: 14400, hours_known: true, hourly_total: 5000,
          points: hours(5000, 0, "unrated"),
        }],
        rm_status: [{ machine_id: 1, machine: "IMM-01", material: null, kg_total: 0, points: [] }],
        shift_rate: [{ machine_id: 1, machine: "IMM-01", priced: false, points: [] }],
      }),
      month({ shift_rate_by_machine: [], total_revenue: 0, total_rate_per_hour: 0 }),
    );
    render(<PlantBoardSection isAdmin />);

    await screen.findByText("No part is priced yet");
    expect(screen.queryByText("₹0")).toBeNull();
  });
});

describe("the figures a customer would check", () => {
  it("groups a month's revenue in lakhs, as their own ledger does", async () => {
    serve(day(), month());
    render(<PlantBoardSection isAdmin />);
    // 1400000 -> 14,00,000, not 1,400,000.
    await waitFor(() =>
      expect(screen.getAllByText(/14,00,000/).length).toBeGreaterThan(0));
    expect(screen.queryByText(/1,400,000/)).toBeNull();
  });

  it("names the material and the day's consumption", async () => {
    serve(day(), month());
    render(<PlantBoardSection isAdmin />);
    await waitFor(() => expect(screen.getAllByText(/4\.75 kg/).length).toBeGreaterThan(0));
  });
});

describe("when the request fails", () => {
  it("shows the backend's own sentence and offers a retry", async () => {
    apiGet.mockRejectedValue(new Error("the workspace has no machines"));
    render(<PlantBoardSection isAdmin />);
    await screen.findByText("the workspace has no machines");
    expect(screen.getByText("Try again")).toBeTruthy();
  });
});

/**
 * The two items from the customer's sheet that the board could not answer:
 * what material came IN, and the plant's own shift-hour rate.
 */
describe("raw material: used and received", () => {
  it("keeps a receipt booked in bags out of the kilogram total", async () => {
    serve(day(), month({
      rm_added: [
        { material: "PP H 110", quantity: 500, unit: "kg", kg: 500 },
        { material: "Masterbatch", quantity: 4, unit: "bags", kg: null },
      ],
      rm_balance: [
        { material: "PP H 110", consumed_kg: 142.56, added_kg: 500,
          added_qty: 500, added_unit: "kg" },
        { material: "Masterbatch", consumed_kg: null, added_kg: null,
          added_qty: 4, added_unit: "bags" },
      ],
    }));
    render(<PlantBoardSection isAdmin={false} />);
    // 500 kg received. The 4 bags are a COUNT; adding them would print a
    // number that is not a weight.
    await screen.findByText(/500 kg received/);
    expect(screen.getByText(/Masterbatch 4 bags/)).toBeTruthy();
    expect(screen.queryByText(/504/)).toBeNull();
  });

  it("says so when nothing received was a weight", async () => {
    serve(day(), month({
      rm_added: [{ material: "Masterbatch", quantity: 4, unit: "bags", kg: null }],
      rm_balance: [{ material: "Masterbatch", consumed_kg: null, added_kg: null,
                     added_qty: 4, added_unit: "bags" }],
    }));
    render(<PlantBoardSection isAdmin={false} />);
    // Not "0 kg received": nothing arrived and what arrived was counted in
    // bags are different facts.
    await screen.findByText(/nothing received was booked in kilograms/);
    expect(screen.queryByText(/0 kg received/)).toBeNull();
  });
});

describe("the plant's shift-hour rate", () => {
  it("is shown, not only the revenue beside it", async () => {
    serve(day(), month());
    render(<PlantBoardSection isAdmin={false} />);
    // total_rate_per_hour has been computed and returned since the board
    // shipped; the card printed the revenue and never the rate.
    await screen.findByText(/₹1,882\/hr/);
  });

  it("still groups the money in lakhs", async () => {
    serve(day(), month());
    render(<PlantBoardSection isAdmin={false} />);
    await screen.findByText(/₹14,00,000/);
    expect(screen.queryByText(/1,400,000/)).toBeNull();
  });
});
