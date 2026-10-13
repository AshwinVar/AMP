import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { GatewayHealth } from "../lib/gateway-health";

/**
 * The thing this UI exists to stop: a machine with no bars today reading as an
 * idle press when the truth is that AMP cannot see it at all.
 *
 * What is pinned here is the wording and the failure behaviour — the VERDICT is
 * the backend's (test_gateway_health.py owns that), and these screens must keep
 * working when the health call does not.
 */

const apiGet = vi.hoisted(() => vi.fn());
vi.mock("../lib/api", () => ({
  apiGet,
  errorDetail: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}));

import { GatewayBanner, MachineLinkBadge, useGatewayHealth } from "./GatewayHealth";

function health(over: Partial<GatewayHealth> = {}): GatewayHealth {
  return {
    as_of: "2026-10-13T12:00:00",
    gateways: [{
      gateway_id: "gw-1", label: "Shrinidhi Pi", site: "", state: "REPORTING",
      light: "green", last_seen: "2026-10-13T11:58:00", quiet_minutes: 2,
      reason: "Shrinidhi Pi is reporting.", fix: "",
    }],
    machines: [{
      machine_id: 1, machine: "IMM-01", status: "Running", state: "REPORTING",
      light: "green", last_heard: "2026-10-13T11:58:00", quiet_minutes: 2,
      gateway: "Shrinidhi Pi", reason: "IMM-01 reported 2 minutes ago.",
      fix: "", resolved_cause: null,
    }],
    reporting: 1, total: 1, worst: "REPORTING", light: "green",
    headline: "All 1 machines are reporting.",
    ...over,
  };
}

const DOWN = health({
  gateways: [{
    gateway_id: "gw-1", label: "Shrinidhi Pi", site: "", state: "GATEWAY UNREACHABLE",
    light: "red", last_seen: "2026-10-13T11:33:00", quiet_minutes: 27,
    reason: "Shrinidhi Pi last reached AMP 27 minutes ago. It has no power, it is off "
          + "the network, or the amp-edge service has stopped.",
    fix: "Check the Pi has power, that it is on the WiFi, and that the amp-edge service is running.",
  }],
  machines: [{
    machine_id: 1, machine: "IMM-01", status: "Running", state: "GATEWAY UNREACHABLE",
    light: "red", last_heard: "2026-10-13T11:33:00", quiet_minutes: 27,
    gateway: "Shrinidhi Pi",
    reason: "Shrinidhi Pi last reached AMP 27 minutes ago.",
    fix: "Check the Pi has power, that it is on the WiFi, and that the amp-edge service is running.",
    resolved_cause: null,
  }],
  reporting: 0, worst: "GATEWAY UNREACHABLE", light: "red",
  headline: "Shrinidhi Pi last reached AMP 27 minutes ago. It has no power, it is off the "
          + "network, or the amp-edge service has stopped — from here those look the same.",
});

function Probe() {
  const h = useGatewayHealth(0);
  return (
    <>
      <GatewayBanner health={h} />
      <MachineLinkBadge health={h} machineId={1} />
    </>
  );
}

beforeEach(() => apiGet.mockReset());

describe("the banner", () => {
  it("stays out of the way when everything is reporting", async () => {
    apiGet.mockResolvedValue(health());
    render(<Probe />);
    // A bar that is always there is a bar nobody reads.
    await screen.findByText("Reporting");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("names all three causes of silence and claims none of them", async () => {
    apiGet.mockResolvedValue(DOWN);
    render(<Probe />);
    const bar = await screen.findByRole("status");
    // A Pi with no power and a Pi off the WiFi look identical from here, so the
    // banner must not pick one.
    expect(bar.textContent).toContain("no power");
    expect(bar.textContent).toContain("off the network");
    expect(bar.textContent).toContain("amp-edge service");
    expect(bar.textContent).toContain("Check the Pi has power");
  });

  it("says what the last outage turned out to be, once it is back", async () => {
    apiGet.mockResolvedValue(health({
      machines: [{
        ...health().machines[0],
        resolved_cause: "The gateway restarted and the counts for that window were never taken.",
      }],
      headline: "All 1 machines are reporting. The gateway restarted and the counts for "
              + "that window were never taken.",
    }));
    render(<Probe />);
    const bar = await screen.findByRole("status");
    expect(bar.textContent).toContain("never taken");
  });
});

describe("the badge", () => {
  it("talks about the LINK, never about the machine", async () => {
    apiGet.mockResolvedValue(DOWN);
    render(<Probe />);
    // "Offline" would read as "the press is off" — the exact confusion this
    // feature exists to end.
    await screen.findByText("Gateway down");
    expect(screen.queryByText(/offline/i)).toBeNull();
  });

  it("shows how long a machine has been quiet when its own link is down", async () => {
    apiGet.mockResolvedValue(health({
      machines: [{
        ...health().machines[0], state: "MACHINE SILENT", light: "amber",
        quiet_minutes: 40,
        reason: "The gateway is reporting, but nothing has arrived for IMM-01 for 40 minutes.",
        fix: "Check the contact wiring at the machine.",
      }],
      reporting: 0, worst: "MACHINE SILENT", light: "amber",
      headline: "The gateway is reporting, but nothing has arrived for IMM-01.",
    }));
    render(<Probe />);
    await screen.findByText(/No signal/);
    expect(screen.getByText(/40m/)).toBeTruthy();
  });

  it("says nothing at all for a machine the backend did not mention", async () => {
    apiGet.mockResolvedValue(health({ machines: [] }));
    render(<Probe />);
    await waitFor(() => expect(apiGet).toHaveBeenCalled());
    expect(screen.queryByText("Reporting")).toBeNull();
  });
});

describe("when the health call does not work", () => {
  // These badges decorate screens whose production figures are still true, so
  // a bad health response must cost the decoration and nothing else.
  it("renders nothing when the call throws", async () => {
    apiGet.mockRejectedValueOnce(new Error("boom"));
    const { container } = render(<Probe />);
    await waitFor(() => expect(apiGet).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });

  it("renders nothing when the answer is not a health payload", async () => {
    // It crashed first: the board went blank because `gateways` was undefined
    // and the banner called .filter on it.
    apiGet.mockResolvedValue({ date: "2026-10-13", production: [] });
    const { container } = render(<Probe />);
    await waitFor(() => expect(apiGet).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });
});
