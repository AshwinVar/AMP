import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The Machine Health list's "Avg health" is the same figure as the Factory
 * Pulse's: the mean over the machines whose score read something. A machine
 * with nothing recorded scores 100 by absence and is left out of the average;
 * its card says so under the score rather than wearing "Healthy" alone.
 */

const apiGet = vi.fn();
vi.mock("../lib/api", () => ({ apiGet: (p: string) => apiGet(p) }));
vi.mock("./DowntimeSnapshot", () => ({ default: () => null }));
vi.mock("./MachineDetailDrawer", () => ({ default: () => null }));

import MachineHealthSection from "./MachineHealthSection";

const OEE = { oee: 0, availability: 0, performance: 0, quality: 0, has_data: false };

function twin(over: Record<string, unknown>) {
  return {
    machine_id: 1, name: "M", line: "", status: "Running", utilization: 60, downtime: "0 min",
    health_score: 100, health_band: "Healthy", health_measured: true, risk_score: 0, risk_level: "Low",
    top_reason: "no major risk indicators", open_maintenance_tasks: 0, pending_agent_actions: 0,
    recent_downtime: [], oee: OEE, ...over,
  };
}

const kpi = (title: string) => screen.getByText(title).parentElement as HTMLElement;

beforeEach(() => apiGet.mockReset());
afterEach(cleanup);

describe("MachineHealthSection average", () => {
  it("averages the measured machines only, and says how many that is", async () => {
    apiGet.mockResolvedValue([
      twin({ machine_id: 1, name: "PRESS-01", health_score: 20, health_band: "Critical" }),
      twin({ machine_id: 2, name: "NEW-01", health_score: 100, health_measured: false }),
    ]);
    render(<MachineHealthSection />);
    await screen.findByText("PRESS-01");
    // 20, not (20 + 100) / 2 = 60.
    expect(kpi("Avg health").textContent).toContain("20 (1 of 2 measured)");
    expect(screen.getByRole("note").textContent).toContain("nothing recorded");
  });

  it("shows a dash when no machine could be measured", async () => {
    apiGet.mockResolvedValue([twin({ machine_id: 2, name: "NEW-01", health_measured: false })]);
    render(<MachineHealthSection />);
    await screen.findByText("NEW-01");
    expect(kpi("Avg health").textContent).toBe("Avg health—");
  });

  it("reads a twin without the field as measured (an older server)", async () => {
    apiGet.mockResolvedValue([twin({ machine_id: 1, name: "PRESS-01", health_score: 58, health_measured: undefined })]);
    render(<MachineHealthSection />);
    await screen.findByText("PRESS-01");
    expect(kpi("Avg health").textContent).toBe("Avg health58");
    expect(screen.queryByRole("note")).toBeNull();
  });
});
