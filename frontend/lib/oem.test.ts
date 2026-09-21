import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  OemRequestError,
  createClaim,
  fetchClaims,
  fetchModels,
  fetchOemNotifications,
  fleetSummary,
  loadFleet,
  get,
  isOemSession,
  post,
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
  // The wire shapes: a naive-UTC reading (no zone) and a date-only warranty end.
  // Neither is parsed here any more — the states beside them are the server's.
  const fiveDaysAgo = "2026-09-16T12:00:00";
  const anHourAgo = "2026-09-21T11:00:00";

  it("counts a machine the server calls silent as offline", () => {
    const s = fleetSummary([machine({ last_seen_at: fiveDaysAgo, reporting: "silent" })]);
    expect(s.offline).toBe(1);
    expect(s.connected).toBe(0);
  });

  it("counts a machine the server calls reporting as connected", () => {
    const s = fleetSummary([machine({ last_seen_at: anHourAgo, reporting: "reporting" })]);
    expect(s.connected).toBe(1);
    expect(s.offline).toBe(0);
  });

  it("counts an UNSHARED machine as unknown, never as offline", () => {
    // The important one. `reporting` is null because the customer did not
    // grant SHARE_MACHINE_HEALTH — not because the machine stopped. Counting it
    // as offline invents a fleet problem out of a privacy setting.
    const s = fleetSummary([machine({ last_seen_at: null, reporting: null })]);
    expect(s.unknown).toBe(1);
    expect(s.offline).toBe(0);
    expect(s.connected).toBe(0);
  });

  it("counts a machine that has never reported as neither silent nor unknown", () => {
    // Health IS shared; the machine simply has not come online yet, which is
    // normal before commissioning. Not a fleet problem, not a privacy setting.
    const s = fleetSummary([machine({ last_seen_at: null, reporting: "never" })]);
    expect(s).toMatchObject({ never: 1, offline: 0, unknown: 0, connected: 0 });
  });

  it("takes the server's verdict and never re-derives it from the timestamp", () => {
    // The bug this replaced: `new Date("2026-09-16T12:00:00")` reads a naive
    // UTC reading in the VIEWER's zone, so the 48-hour line moved with whoever
    // was looking. The reading here is five days old and the server says
    // reporting; the server decided, so this side does not second-guess it.
    const s = fleetSummary([machine({ last_seen_at: fiveDaysAgo, reporting: "reporting" })]);
    expect(s.connected).toBe(1);
    expect(s.offline).toBe(0);
    // CONTROL: and a fresh reading the server calls silent is silent.
    expect(fleetSummary([machine({ last_seen_at: anHourAgo, reporting: "silent" })]).offline).toBe(1);
  });

  it("counts a reading with no verdict beside it as unknown, not as reporting", () => {
    // A server that does not say (the field is absent) is not a reason to
    // invent an answer from the date on this side.
    const s = fleetSummary([machine({ last_seen_at: anHourAgo })]);
    expect(s).toMatchObject({ unknown: 1, connected: 0, offline: 0 });
  });

  it("keeps the states separate across a mixed fleet", () => {
    const s = fleetSummary([
      machine({ serial_number: "a", last_seen_at: anHourAgo, reporting: "reporting" }),
      machine({ serial_number: "b", last_seen_at: fiveDaysAgo, reporting: "silent" }),
      machine({ serial_number: "c", last_seen_at: null, reporting: null }),
      machine({ serial_number: "d", last_seen_at: null, reporting: "never" }),
    ]);
    expect(s).toMatchObject({ total: 4, connected: 1, offline: 1, unknown: 1, never: 1 });
  });

  it("counts warranty from the server's verdict only", () => {
    const s = fleetSummary([
      machine({ serial_number: "a", warranty_end: "2028-01-01", warranty: "active" }),
      machine({ serial_number: "b", warranty_end: "2026-06-01", warranty: "expired" }),
      machine({ serial_number: "c", warranty_end: null, warranty: "unknown" }), // never recorded
      machine({ serial_number: "d", warranty_end: "2029-06-01", warranty: "not_started" }),
    ]);
    // Exactly one is in warranty. The unrecorded one is NOT counted as covered —
    // that is a commercial claim nobody made — and neither is one not yet begun.
    expect(s.warrantyActive).toBe(1);
  });

  it("does not decide 'covered' from an end date on its own", () => {
    // `new Date("2028-01-01")` is UTC MIDNIGHT — the START of the last covered
    // day — so the old comparison called every warranty over a day early while
    // the server, asked by the machine's own drawer, said "active". A date with
    // no verdict beside it counts for nothing here.
    const s = fleetSummary([machine({ warranty_end: "2028-01-01" })]);
    expect(s.warrantyActive).toBe(0);
  });

  it("handles an empty fleet without inventing anything", () => {
    expect(fleetSummary([])).toMatchObject({
      total: 0,
      connected: 0,
      offline: 0,
      never: 0,
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

describe("a structured refusal (ADR-0021 contract routes)", () => {
  // The contract routes refuse with an OBJECT detail — {field, message} for a
  // bad term, {withheld: true, reason} when the factory has withdrawn consent,
  // {message, problems} for coverage. String(detail) of any of those is
  // "[object Object]", which tells a manufacturer nothing about what to fix.
  it("keeps the detail and reads its message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      respond({ detail: { field: "sla_target_pct", message: "must be at most 100.00" } }, 422),
    );
    const refusal = await post("/oem/contracts", {}).catch((e: OemRequestError) => e) as OemRequestError;
    expect(refusal).toBeInstanceOf(OemRequestError);
    expect(refusal.status).toBe(422);
    expect(refusal.message).toBe("sla_target_pct: must be at most 100.00");
    expect(refusal.detail).toEqual({ field: "sla_target_pct", message: "must be at most 100.00" });
  });

  it("names the withheld reason, and lists coverage problems", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(respond({ detail: { withheld: true, reason: "sharing withdrawn by factory",
                                                 message: "withheld until it does" } }, 403))
      .mockResolvedValueOnce(respond({ detail: { message: "Coverage cannot be accepted",
                                                 problems: ["SN-1 is not linked"] } }, 409));
    const withheld = await get("/oem/contracts/1/statements/2").catch((e: OemRequestError) => e) as OemRequestError;
    expect(withheld.status).toBe(403);
    expect((withheld.detail as { withheld: boolean }).withheld).toBe(true);
    expect(withheld.message).toBe("withheld until it does");
    const coverage = await post("/oem/contracts/1/propose", {}).catch((e: OemRequestError) => e) as OemRequestError;
    expect(coverage.message).toBe("Coverage cannot be accepted: SN-1 is not linked");
  });
});

describe("loadFleet walks the fleet a page at a time (ADR-0036)", () => {
  function page(ids: number[], total: number) {
    return { total, limit: 100, offset: 0, machines: ids.map((id) => machine({ installation_id: id, serial_number: `SN-${id}` })) };
  }

  it("holds one page when that is all the screen asked for, and reports the fleet's total", async () => {
    const fetchPage = vi.fn(async () => page(Array.from({ length: 100 }, (_, i) => i + 1), 250));
    const fleet = await loadFleet("FACTORY_A", 100, fetchPage);
    expect(fleet.machines).toHaveLength(100);
    expect(fleet.total).toBe(250);
    expect(fetchPage).toHaveBeenCalledTimes(1);
    expect(fetchPage).toHaveBeenCalledWith("FACTORY_A", 100, 0);
  });

  it("asks for the next page with the offset when the screen wants more, and stops at the total", async () => {
    const all = Array.from({ length: 250 }, (_, i) => i + 1);
    const fetchPage = vi.fn(async (_c?: string, limit = 100, offset = 0) =>
      page(all.slice(offset, offset + limit), 250));
    const fleet = await loadFleet(undefined, 200, fetchPage);
    expect(fleet.machines).toHaveLength(200);
    expect(fetchPage.mock.calls.map((c) => c[2])).toEqual([0, 100]);
    const whole = await loadFleet(undefined, 1000, fetchPage);
    expect(whole.machines).toHaveLength(250);
    expect(whole.total).toBe(250);
  });

  it("keeps a row that shifted between pages once, and stops on a server that ignores the offset", async () => {
    const fetchPage = vi.fn(async () => page(Array.from({ length: 100 }, (_, i) => i + 1), 500));
    const fleet = await loadFleet(undefined, 300, fetchPage);
    expect(fleet.machines).toHaveLength(100);
    expect(fetchPage).toHaveBeenCalledTimes(2);     // the second page added nothing, so it stopped
  });
});
