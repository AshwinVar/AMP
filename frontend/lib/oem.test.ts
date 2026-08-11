import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fleetSummary, isOemSession, shareable, type FleetMachine } from "./oem";

/**
 * The OEM portal's client logic (ADR-0017).
 *
 * The property worth testing here is not formatting — it is that the UI never
 * turns a PRIVACY SETTING into an OPERATIONAL FACT. A customer who declines to
 * share operating hours must not appear on a manufacturer's screen as a machine
 * with zero hours, and a customer who declines to share health must not appear
 * as a machine that has gone offline. Both would send an engineer to a site
 * where nothing is wrong.
 */

function machine(over: Partial<FleetMachine> = {}): FleetMachine {
  return {
    installation_id: 1,
    serial_number: "SN-1",
    model_code: "X200",
    model_name: "X200",
    customer: "FACTORY_A",
    site: "Plant 1",
    lifecycle_status: "Active",
    installed_at: null,
    commissioned_at: null,
    warranty_start: null,
    warranty_end: null,
    operating_hours: null,
    last_seen_at: null,
    machine_status: null,
    utilization: null,
    shared: [],
    ...over,
  };
}

function tokenWith(payload: Record<string, unknown>) {
  const body = btoa(JSON.stringify(payload));
  return `header.${body}.signature`;
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("isOemSession", () => {
  it("recognises an OEM token by its principal claim", () => {
    localStorage.setItem("token", tokenWith({ principal: "oem", oem: "OEM_ALPHA" }));
    expect(isOemSession()).toBe(true);
  });

  it("does not treat a factory token as an OEM session", () => {
    localStorage.setItem("token", tokenWith({ tenant: "FACTORY_A", role: "Admin" }));
    expect(isOemSession()).toBe(false);
  });

  it("does not mistake a factory Admin for an OEM just because of a role", () => {
    // The claim, not the role string. A factory "Admin" is not an OEM admin.
    localStorage.setItem("token", tokenWith({ role: "OEM_ADMIN", tenant: "FACTORY_A" }));
    expect(isOemSession()).toBe(false);
  });

  it("is false with no token, and does not throw on a malformed one", () => {
    expect(isOemSession()).toBe(false);
    localStorage.setItem("token", "not-a-jwt");
    expect(isOemSession()).toBe(false);
  });
});

describe("shareable", () => {
  it("says 'not shared' rather than showing a zero", () => {
    // The bug this prevents: a machine whose owner declined to share hours
    // rendering as "0 h", which reads as a brand-new or broken machine.
    expect(shareable(null, false, " h")).toBe("not shared");
    expect(shareable(0, false, " h")).toBe("not shared");
  });

  it("distinguishes 'not shared' from 'shared but no reading yet'", () => {
    expect(shareable(null, true, " h")).toBe("no data");
    expect(shareable(null, false, " h")).toBe("not shared");
  });

  it("shows a real value when it was shared", () => {
    expect(shareable(1850, true, " h")).toBe("1850 h");
    expect(shareable(0, true, " h")).toBe("0 h"); // a real, measured zero
  });
});

describe("fleetSummary", () => {
  const recent = new Date(Date.now() - 3600 * 1000).toISOString();
  const old = new Date(Date.now() - 5 * 24 * 3600 * 1000).toISOString();

  it("counts a machine that has not reported for days as offline", () => {
    const s = fleetSummary([machine({ last_seen_at: old })]);
    expect(s.offline).toBe(1);
    expect(s.connected).toBe(0);
  });

  it("counts a recently-seen machine as connected", () => {
    const s = fleetSummary([machine({ last_seen_at: recent })]);
    expect(s.connected).toBe(1);
    expect(s.offline).toBe(0);
  });

  it("counts an UNSHARED machine as unknown, never as offline", () => {
    // The important one. `last_seen_at` is null because the customer did not
    // grant SHARE_MACHINE_HEALTH — not because the machine stopped. Counting it
    // as offline invents a fleet problem out of a privacy setting.
    const s = fleetSummary([machine({ last_seen_at: null })]);
    expect(s.unknown).toBe(1);
    expect(s.offline).toBe(0);
    expect(s.connected).toBe(0);
  });

  it("keeps the three states separate across a mixed fleet", () => {
    const s = fleetSummary([
      machine({ serial_number: "a", last_seen_at: recent }),
      machine({ serial_number: "b", last_seen_at: old }),
      machine({ serial_number: "c", last_seen_at: null }),
    ]);
    expect(s).toMatchObject({ total: 3, connected: 1, offline: 1, unknown: 1 });
  });

  it("counts warranty from the recorded end date only", () => {
    const future = new Date(Date.now() + 90 * 24 * 3600 * 1000)
      .toISOString()
      .slice(0, 10);
    const past = new Date(Date.now() - 90 * 24 * 3600 * 1000)
      .toISOString()
      .slice(0, 10);
    const s = fleetSummary([
      machine({ serial_number: "a", warranty_end: future }),
      machine({ serial_number: "b", warranty_end: past }),
      machine({ serial_number: "c", warranty_end: null }), // never recorded
    ]);
    // Exactly one is in warranty. The unrecorded one is NOT counted as covered —
    // that is a commercial claim nobody made.
    expect(s.warrantyActive).toBe(1);
  });

  it("handles an empty fleet without inventing anything", () => {
    expect(fleetSummary([])).toMatchObject({
      total: 0,
      connected: 0,
      offline: 0,
      unknown: 0,
      warrantyActive: 0,
    });
  });
});
