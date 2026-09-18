import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", () => ({ apiGet: vi.fn() }));

import ScorecardStrip from "./ScorecardStrip";
import { apiGet } from "../lib/api";

/**
 * The Plant OEE tile says how much of the plant it measured (OEE contract s4;
 * backend test_scorecard_oee_states_coverage.py). A machine that stops reporting
 * leaves the pooled figure, so the tile reads HIGHER the week it goes silent; the
 * tile must say "from 2 of 3 machines" then, and say nothing extra when every
 * machine reported or when there is no figure at all.
 */

const oee = (over: Record<string, unknown> = {}) => ({
  key: "oee", label: "Plant OEE", value: 81, unit: "%", tone: "warn",
  delta: 9, delta_tone: "good",
  coverage: { machines_expected: 3, machines_reporting: 2, coverage_pct: 67, complete: false },
  ...over,
});

function serve(kpis: unknown[]) {
  vi.mocked(apiGet).mockResolvedValue({ has_data: true, kpis });
}

describe("ScorecardStrip plant OEE coverage", () => {
  beforeEach(() => vi.mocked(apiGet).mockReset());

  it("says the figure came from part of the plant", async () => {
    serve([oee()]);
    render(<ScorecardStrip />);
    expect(await screen.findByText("81%")).toBeTruthy();
    expect(screen.getByText(/from 2 of 3 machines/)).toBeTruthy();
  });

  it("says nothing extra when every machine reported", async () => {
    serve([oee({ coverage: { machines_expected: 3, machines_reporting: 3, coverage_pct: 100, complete: true } })]);
    render(<ScorecardStrip />);
    expect(await screen.findByText("81%")).toBeTruthy();
    expect(screen.queryByText(/ of 3 machine/)).toBeNull();
  });

  it("says nothing about coverage under a figure that was not measured", async () => {
    serve([oee({ value: null, tone: "none", delta: null, delta_tone: null,
                 coverage: { machines_expected: 3, machines_reporting: 0, coverage_pct: 0, complete: false } })]);
    render(<ScorecardStrip />);
    expect(await screen.findByText("—")).toBeTruthy();
    expect(screen.queryByText(/ of 3 machine/)).toBeNull();
  });

  it("uses the singular for a one-machine plant", async () => {
    serve([oee({ coverage: { machines_expected: 1, machines_reporting: 0, coverage_pct: 0, complete: false } })]);
    render(<ScorecardStrip />);
    expect(await screen.findByText(/from 0 of 1 machine$/)).toBeTruthy();
  });
});
