import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", () => ({ apiGet: vi.fn(), getUserRole: () => "Admin" }));
vi.mock("./UnitRateEditor", () => ({ default: () => null }));

import MoneyStorySnapshot from "./MoneyStorySnapshot";
import { apiGet } from "../lib/api";

/**
 * The money-story card values the gap between plant OEE and world class. A
 * machine that stops reporting leaves the pooled OEE (OEE contract s4): the gap
 * narrows and the per-year upside shrinks the week the worst machine goes
 * silent. The card says "OEE from 2 of 3 machines" then (backend
 * test_exec_oee_and_recovery_state_coverage.py), and nothing extra when every
 * machine reported.
 */

const recovery = (over: Record<string, unknown> = {}) => ({
  has_data: true, oee: 72, world_class: 85, gap_points: 13, unit_value_gbp: null,
  recoverable_units_per_year: 5200, recoverable_value_per_year: null,
  oee_trend: "new", oee_points_delta: null, biggest_lever: null, lever_label: null,
  lever_recoverable_value_per_year: null, lever_recoverable_units_per_year: 0,
  coverage: { machines_expected: 3, machines_reporting: 2, coverage_pct: 67, complete: false },
  ...over,
});

function serve(rec: Record<string, unknown>) {
  vi.mocked(apiGet).mockImplementation(async (path: string) => {
    if (path === "/recovery-summary") return rec;
    return { total_downtime_minutes: 0, estimated_loss_units: 0, estimated_loss_value: null, unit_value_gbp: null };
  });
}

describe("MoneyStorySnapshot plant OEE coverage", () => {
  beforeEach(() => vi.mocked(apiGet).mockReset());

  it("says the OEE it values came from part of the plant", async () => {
    serve(recovery());
    render(<MoneyStorySnapshot />);
    expect(await screen.findByText(/OEE from 2 of 3 machines/)).toBeTruthy();
  });

  it("says nothing extra when every machine reported", async () => {
    serve(recovery({ coverage: { machines_expected: 3, machines_reporting: 3, coverage_pct: 100, complete: true } }));
    render(<MoneyStorySnapshot />);
    expect(await screen.findByText(/closing OEE 72%/)).toBeTruthy();
    expect(screen.queryByText(/ of 3 machine/)).toBeNull();
  });
});
