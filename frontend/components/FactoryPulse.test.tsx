import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * "Fleet health" is the average over the machines whose score read something.
 *
 * A machine with nothing recorded in the risk window scores 100 by absence
 * (every history rule reads nothing and takes no points off), and the header
 * used to average those 100s in at full weight — so a fleet looked healthier
 * the less it reported, and an empty fleet read "Fleet health 0", the worst
 * score there is. The backend (ai/pulse.py) now sends null when nothing could
 * be measured and how many machines the average covers; this tile says both.
 */

const apiGet = vi.fn();
vi.mock("../lib/api", () => ({ apiGet: (p: string) => apiGet(p) }));

import FactoryPulse from "./FactoryPulse";

function pulse(fleet: Record<string, unknown>) {
  return {
    fleet: { machines: 2, measured: 2, avg_health: 71, needs_attention: 0, worst: null, ...fleet },
    agents: { agents_active: 1, actions_7d: 3, auto_rate: 50, awaiting_you: 0 },
    headline: "Fleet health 71 · all clear",
  };
}

const tile = (label: string) =>
  screen.getByText(label).parentElement as HTMLElement;

beforeEach(() => apiGet.mockReset());
afterEach(cleanup);

describe("FactoryPulse fleet health", () => {
  it("shows the average with no coverage note when every machine was measured", async () => {
    apiGet.mockResolvedValue(pulse({}));
    render(<FactoryPulse />);
    await screen.findByText("Fleet health");
    expect(tile("Fleet health").textContent).toBe("Fleet health71");
  });

  it("says how many machines the average covers when some read nothing", async () => {
    apiGet.mockResolvedValue(pulse({ machines: 3, measured: 2, avg_health: 71 }));
    render(<FactoryPulse />);
    await screen.findByText("Fleet health");
    expect(tile("Fleet health").textContent).toContain("2 of 3 measured");
    expect(tile("Fleet health").textContent).toContain("71");
  });

  it("shows a dash, not a number, when nothing has been recorded for any machine", async () => {
    apiGet.mockResolvedValue(pulse({ machines: 3, measured: 0, avg_health: null }));
    render(<FactoryPulse />);
    await screen.findByText("Fleet health");
    const text = tile("Fleet health").textContent ?? "";
    expect(text).toContain("—");
    expect(text).toContain("nothing recorded yet");
    expect(text).not.toMatch(/\b(0|100)\b/);
  });

  it("says 'no machines yet' for an empty fleet, never 0", async () => {
    apiGet.mockResolvedValue(pulse({ machines: 0, measured: 0, avg_health: null }));
    render(<FactoryPulse />);
    await screen.findByText("Fleet health");
    const text = tile("Fleet health").textContent ?? "";
    expect(text).toContain("no machines yet");
    expect(text).not.toMatch(/\b0\b/);
  });

  it("treats a pulse without the coverage field as fully measured (an older server)", async () => {
    apiGet.mockResolvedValue(pulse({ measured: undefined }));
    render(<FactoryPulse />);
    await screen.findByText("Fleet health");
    expect(tile("Fleet health").textContent).toBe("Fleet health71");
  });
});
