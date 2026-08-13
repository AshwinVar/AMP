import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  OemRequestError,
  createClaim,
  fetchClaims,
  fetchModels,
  fetchOemNotifications,
  fleetSummary,
  isOemSession,
  registerMachine,
  revokeClaim,
  shareable,
  type FleetMachine,
} from "./oem";

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

/**
 * The request layer, which until now had no tests at all.
 *
 * These are thin wrappers, and the temptation is to call them too trivial to
 * test. Two of the three properties below are exactly the kind a thin wrapper
 * gets wrong: a POST that silently succeeds on a 409, and a refusal whose STATUS
 * is thrown away — the portal decides "you are in the wrong portal" (401) from
 * "that machine is not yours" (404) by reading it.
 */
function respond(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe("talking to /oem", () => {
  it("registers a machine and issues a claim as POSTs, carrying the body", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(respond({ installation_id: 4, serial_number: "SN-9" }));
    localStorage.setItem("token", tokenWith({ principal: "oem" }));

    await registerMachine({ serial_number: "SN-9", model_id: 2 });
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/oem\/machines$/);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      serial_number: "SN-9",
      model_id: 2,
    });

    await createClaim(4, 14);
    expect(fetchMock.mock.calls[1][0]).toMatch(/\/oem\/machines\/4\/claim$/);
    expect(JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body)))
      .toEqual({ expires_in_days: 14 });

    // An omitted expiry is sent as null, NOT dropped: the server chooses the
    // default, and a missing key would leave that decision to whatever the
    // request model happens to default to on the next release.
    await createClaim(4);
    expect(JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body)))
      .toEqual({ expires_in_days: null });

    await revokeClaim(11);
    expect(fetchMock.mock.calls[3][0]).toMatch(/\/oem\/claims\/11\/revoke$/);
  });

  it("reads the catalogue, the claims and the notifications as GETs", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(respond([]));
    await fetchModels();
    await fetchClaims();
    await fetchOemNotifications();
    const paths = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(paths[0]).toMatch(/\/oem\/models$/);
    expect(paths[1]).toMatch(/\/oem\/claims$/);
    expect(paths[2]).toMatch(/\/oem\/notifications$/);
    for (const call of fetchMock.mock.calls) {
      expect((call[1] as RequestInit)?.method ?? "GET").toBe("GET");
    }
  });

  it("throws a REFUSAL that keeps the status and the server's own sentence", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond({ detail: "That claim code is not valid." }, 404),
    );
    await expect(createClaim(4)).rejects.toMatchObject({
      status: 404,
      message: "That claim code is not valid.",
    });
    // CONTROL: a POST that fails must REJECT, not resolve with the error body —
    // the caller shows "added" on a resolved promise.
    await expect(createClaim(4)).rejects.toBeInstanceOf(OemRequestError);
  });

  it("keeps the status when the failure body is not JSON", async () => {
    // A 502 from a proxy has an HTML body. Losing the status here would make an
    // outage indistinguishable from a refusal.
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false,
      status: 502,
      json: async () => {
        throw new Error("not json");
      },
    } as unknown as Response);
    await expect(fetchClaims()).rejects.toMatchObject({ status: 502 });
    await expect(registerMachine({ serial_number: "X", model_id: 1 }))
      .rejects.toMatchObject({ status: 502 });
  });
});
