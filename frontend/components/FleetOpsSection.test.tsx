import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Fleet Control's three obligations:
 *  - the state poll renders the roster, the KPIs and the floor map;
 *  - the assistance queue offers the closed advice vocabulary to a role the
 *    server would accept it from, and only to such a role (an Operator used
 *    to be the classic two-buttons-that-403 case elsewhere);
 *  - an empty fleet offers the simulated seed to an Admin, and the click
 *    posts to /fleet/seed.
 */

const apiGet = vi.fn();
const apiPost = vi.fn();
let role = "Admin";

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
  errorDetail: (e: unknown) => (e instanceof Error ? e.message : String(e)),
  getUserRole: () => role,
}));

import FleetOpsSection from "./FleetOpsSection";

const STATE = {
  stations: {
    Warehouse: { x: 8, y: 15, kind: "anchor" },
    Packing: { x: 92, y: 30, kind: "anchor" },
    "IMM-01": { x: 50, y: 50, kind: "machine", machine_status: "Running" },
  },
  vehicles: [
    { id: 1, name: "AMR-01", vehicle_type: "AMR", status: "Carrying", x: 40, y: 40,
      battery_pct: 76.5, current_task_id: 9, odometer_m: 1234, deliveries_total: 3 },
    { id: 2, name: "FORK-01", vehicle_type: "Forklift", status: "Stuck", x: 60, y: 60,
      battery_pct: 15.0, current_task_id: 10, odometer_m: 500, deliveries_total: 1 },
  ],
  tasks: [
    { id: 9, origin: "IMM-01", destination: "Packing", payload: "Finished batch",
      priority: 2, status: "InTransit", vehicle_id: 1, source: "mes" },
    { id: 10, origin: "Warehouse", destination: "IMM-01", payload: "Raw material",
      priority: 2, status: "Assigned", vehicle_id: 2, source: "manual" },
  ],
  alerts: [
    { id: 4, vehicle_id: 2, task_id: 10, kind: "Stuck", severity: "High",
      message: "FORK-01 stopped at (60, 60) en route to IMM-01", status: "Open" },
  ],
  kpis: {
    fleet_size: 2, by_status: { Carrying: 1, Stuck: 1 }, pending_tasks: 0,
    active_tasks: 2, delivered_24h: 7, avg_wait_s: 12.0, open_alerts: 1,
    avg_battery_pct: 45.8, distance_total_m: 1734,
  },
};

afterEach(cleanup);

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  role = "Admin";
  apiGet.mockImplementation((p: string) =>
    p === "/fleet/state" ? Promise.resolve(STATE) : Promise.resolve(null));
  apiPost.mockResolvedValue({});
});

describe("FleetOpsSection", () => {
  it("renders the roster, KPIs and the plea from one state poll", async () => {
    render(<FleetOpsSection />);
    expect(await screen.findByText("Delivered (24h)")).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
    // roster rows (names also appear as map labels, so expect >= 1 each)
    expect(screen.getAllByText("AMR-01").length).toBeGreaterThan(0);
    expect(screen.getAllByText("FORK-01").length).toBeGreaterThan(0);
    expect(screen.getByText(/stopped at \(60, 60\)/)).toBeTruthy();
  });

  it("advising a plea posts the verb and refreshes", async () => {
    render(<FleetOpsSection />);
    const resume = await screen.findByRole("button", { name: "Resume" });
    fireEvent.click(resume);
    await screen.findByText("Fleet Control");
    expect(apiPost).toHaveBeenCalledWith("/fleet/alerts/4/advise", { advice: "resume" });
  });

  it("offers an Operator no advice buttons, and says why", async () => {
    role = "Operator";
    render(<FleetOpsSection />);
    await screen.findByText(/stopped at/);
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    expect(screen.getByRole("note").textContent).toMatch(/Supervisor or Admin/);
  });

  it("an empty fleet offers the Admin the simulated seed", async () => {
    apiGet.mockImplementation(() => Promise.resolve({
      ...STATE, vehicles: [], tasks: [], alerts: [],
      kpis: { ...STATE.kpis, fleet_size: 0, open_alerts: 0 },
    }));
    render(<FleetOpsSection />);
    const seed = await screen.findByRole("button", { name: "Seed demo fleet" });
    fireEvent.click(seed);
    await screen.findByText("Fleet Control");
    expect(apiPost).toHaveBeenCalledWith("/fleet/seed", {});
  });

  it("a failed first poll shows the server's own sentence", async () => {
    // Once, not persistent: vitest 4 flags a persistently rejecting mock as
    // an unhandled rejection when the poll fires again after the test.
    apiGet.mockRejectedValueOnce(new Error("Your plan does not include the fleet pack"));
    render(<FleetOpsSection />);
    expect(await screen.findByText(/fleet pack/)).toBeTruthy();
  });
});
