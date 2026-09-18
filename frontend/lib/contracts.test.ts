import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BUCKETS,
  RESOLUTION_BUCKETS,
  acceptBlockers,
  acceptanceByRevision,
  bucketLabel,
  bucketShares,
  closedPeriod,
  contractError,
  contractStateLabel,
  currentAcceptance,
  describeCause,
  evidenceLines,
  fetchStatementBytes,
  formatDuration,
  liveDisputes,
  parseUtcInput,
  sharesTileCovered,
  shortHash,
  slaStateLabel,
  termsTemplate,
  verifyCanonical,
  type Acceptance,
  type StatementPayload,
  type Totals,
} from "./contracts";
import { OemRequestError } from "./oem";

/**
 * The shared client logic of agreed downtime attribution (ADR-0020).
 *
 * What is worth pinning here is honesty, not layout:
 *   * UNMEASURED always reads "No data" — never uptime, never downtime;
 *   * durations are exact integer seconds, and the buckets tile the covered time;
 *   * "Check consistency" hashes the EXACT downloaded bytes with WebCrypto, so a
 *     party's own copy can be checked against the hash both parties accepted;
 *   * an acceptance is shown valid only for the revision it names, and Accept is
 *     blocked, with the reason, whenever the server would refuse it.
 */

afterEach(() => {
  vi.restoreAllMocks();
});

const TOTALS: Totals = {
  covered_seconds: 2_678_400,
  available_seconds: 2_600_000,
  oem_seconds: 7_200,
  factory_seconds: 36_000,
  disputed_seconds: 3_600,
  unmeasured_seconds: 31_600,
};

describe("the five buckets", () => {
  it("are the backend's, in its order", () => {
    expect([...BUCKETS]).toEqual(["AVAILABLE", "OEM", "FACTORY", "DISPUTED", "UNMEASURED"]);
    expect([...RESOLUTION_BUCKETS]).toEqual(["AVAILABLE", "OEM", "FACTORY", "UNMEASURED"]);
  });

  it("call unmeasured time 'No data', never uptime or downtime", () => {
    expect(bucketLabel("UNMEASURED")).toBe("No data");
    expect(bucketLabel("OEM")).toBe("OEM (machine fault)");
    expect(bucketLabel("FACTORY")).toBe("Factory");
    expect(bucketLabel("AVAILABLE")).toBe("Available");
    expect(bucketLabel("DISPUTED")).toBe("Disputed");
    expect(bucketLabel("SOMETHING_NEW")).toBe("SOMETHING_NEW");
  });
});

describe("formatDuration: exact integer seconds", () => {
  it("formats hours, minutes and seconds without rounding", () => {
    expect(formatDuration(0)).toBe("0 s");
    expect(formatDuration(59)).toBe("59 s");
    expect(formatDuration(60)).toBe("1 min");
    expect(formatDuration(125)).toBe("2 min 05 s");
    expect(formatDuration(3600)).toBe("1 h 00 min");
    expect(formatDuration(3725)).toBe("1 h 02 min 05 s");
    expect(formatDuration(90061)).toBe("25 h 01 min 01 s");
    expect(formatDuration(2_678_400)).toBe("744 h 00 min");
  });

  it("refuses to invent a duration from a non-integer or negative value", () => {
    for (const bad of [-1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, 2 ** 60]) {
      expect(formatDuration(bad), String(bad)).toBe("—");
    }
  });
});

describe("the bucket tiles add up to the covered time", () => {
  it("gives each bucket its seconds and share", () => {
    const shares = bucketShares(TOTALS);
    expect(shares.map((s) => s.bucket)).toEqual([...BUCKETS]);
    expect(shares.map((s) => s.seconds)).toEqual([2_600_000, 7_200, 36_000, 3_600, 31_600]);
    expect(shares.reduce((a, s) => a + s.seconds, 0)).toBe(TOTALS.covered_seconds);
    expect(shares[4].share).toBeCloseTo(31_600 / 2_678_400, 10);
    expect(sharesTileCovered(TOTALS)).toBe(true);
  });

  it("says when they do not, rather than drawing a bar that lies", () => {
    expect(sharesTileCovered({ ...TOTALS, unmeasured_seconds: 0 })).toBe(false);
  });

  it("draws nothing for a period with no covered time", () => {
    const empty = { covered_seconds: 0, available_seconds: 0, oem_seconds: 0,
                    factory_seconds: 0, disputed_seconds: 0, unmeasured_seconds: 0 };
    expect(bucketShares(empty).every((s) => s.share === 0)).toBe(true);
    expect(sharesTileCovered(empty)).toBe(true);
  });
});

describe("labels", () => {
  it("names SLA states, and never calls an unevaluable period a pass", () => {
    expect(slaStateLabel("met")).toBe("SLA met");
    expect(slaStateLabel("breached")).toBe("SLA breached");
    expect(slaStateLabel("pending_disputes")).toBe("Pending disputes");
    expect(slaStateLabel("not_evaluable")).toBe("Not evaluable");
    expect(slaStateLabel(null)).toBe("Not evaluable");
    expect(slaStateLabel("weird")).toBe("weird");
  });

  it("names contract states", () => {
    expect(contractStateLabel("proposed")).toBe("Awaiting factory acceptance");
    expect(contractStateLabel("active")).toBe("Active");
    expect(contractStateLabel("not_started")).toBe("Not started");
    expect(contractStateLabel("terminated")).toBe("Terminated");
    expect(contractStateLabel("mystery")).toBe("mystery");
  });

  it("explains every cause the engine writes", () => {
    expect(describeCause("status:Running")).toBe("Reported Running");
    expect(describeCause("status:Idle+Running")).toBe("Reported Idle+Running");
    expect(describeCause("status_default:Breakdown")).toBe(
      "Breakdown with no explicit reason (the agreed default)");
    expect(describeCause("reason:no material")).toBe("Factory reason: no material");
    expect(describeCause("no_telemetry")).toBe("No trusted telemetry received");
    expect(describeCause("conflicting_status")).toBe("Trusted sources disagreed");
    expect(describeCause("unrecognised_status")).toBe("A status the terms do not recognise");
    expect(describeCause("unmapped_reason")).toBe("A reason the terms do not map");
    expect(describeCause("conflicting_reasons")).toBe("Reasons for both parties");
    expect(describeCause("installation_unlinked")).toBe(
      "Installation unlinked from the accepted machine");
    expect(describeCause("open_dispute:#4")).toBe("Under dispute #4");
    expect(describeCause("agreed_override:#4")).toBe("Agreed resolution of dispute #4");
    expect(describeCause("brand_new_cause")).toBe("brand_new_cause");
  });

  it("summarises evidence without inventing any", () => {
    expect(evidenceLines({
      telemetry_spans: [{ id: 3, source: "mqtt", status: "Breakdown",
                          start: "2026-05-01T00:00:00Z", end: "2026-05-01T01:00:00Z" }],
      downtime_logs: [{ id: 9, at: "2026-05-01T00:05:00Z", reason: "No material" }],
    })).toEqual([
      "mqtt span #3: Breakdown 2026-05-01T00:00:00Z to 2026-05-01T01:00:00Z",
      "downtime log #9 at 2026-05-01T00:05:00Z: “No material”",
    ]);
    expect(evidenceLines({ disputes: [{ id: 2, status: "open", resolution_bucket: null }] }))
      .toEqual(["dispute #2: open"]);
    expect(evidenceLines({ disputes: [{ id: 2, status: "resolved", resolution_bucket: "FACTORY" }] }))
      .toEqual(["dispute #2: resolved as Factory"]);
    expect(evidenceLines({ linkage: { coverage_ended_at: "2026-05-02T00:00:00Z",
                                      reason: "machine_unlinked" } }))
      .toEqual(["coverage ended 2026-05-02T00:00:00Z (machine_unlinked)"]);
    expect(evidenceLines({ telemetry_spans: [] })).toEqual(["no telemetry span held this time"]);
    expect(evidenceLines({})).toEqual([]);
    expect(evidenceLines(null)).toEqual([]);
  });

  it("shortens a hash and marks a missing one", () => {
    expect(shortHash("3ef65c41cac109befb92d5ad6ab272074b1c06b0e78fb1958ba42bac66e69aa3"))
      .toBe("3ef65c41cac1…");
    expect(shortHash(null)).toBe("—");
  });
});

function acceptance(over: Partial<Acceptance> = {}): Acceptance {
  return { party: "OEM", actor: "oem:A:admin", accepted_at: "2026-06-02T10:00:00Z",
           content_hash: "a".repeat(64), revision: 1, valid: false, ...over };
}

function statement(over: Partial<StatementPayload> = {}): StatementPayload {
  return {
    id: 5, contract_id: 1, term_version_id: 1,
    period_start: "2026-05-01T00:00:00Z", period_end: "2026-06-01T00:00:00Z",
    revision: 3, content_hash: "c".repeat(64), computed_at: "2026-06-01T00:10:00Z",
    computed_by_party: "OEM", computed_by: "oem:A:admin",
    content: {
      schema: "amp.downtime-attribution-statement/1",
      contract: { id: 1, ref: "AMC-1", oem_code: "A", factory_tenant_code: "F",
                  terms_version: 1, terms_hash: "t".repeat(64) },
      period: { start: "2026-05-01T00:00:00Z", end: "2026-06-01T00:00:00Z",
                timezone: "Asia/Kolkata" },
      machines: [], totals: TOTALS,
      sla: { target_pct: "97.00", min_measured_pct: "90.00", measured_pct: "98.82",
             base_seconds: 2_610_800, availability_pct: "99.72",
             availability_if_disputes_oem: null, availability_if_disputes_factory: null,
             availability_if_disputes_available: null, state: "met",
             not_evaluable_reason: null },
      credit: { currency: "INR", period_fee: "40000.00", tier_below_pct: null,
                credit_pct: "0.00", amount: "0.00", range_min: null, range_max: null },
    },
    content_purged: false, acceptances: [], agreed: false, disputes: [],
    ...over,
  };
}

describe("acceptances are bound to a revision", () => {
  it("groups them newest revision first, keeping cancelled ones visible", () => {
    const rows = [
      acceptance({ revision: 1, party: "OEM", valid: false }),
      acceptance({ revision: 3, party: "FACTORY", valid: true }),
      acceptance({ revision: 1, party: "FACTORY", valid: false }),
    ];
    const grouped = acceptanceByRevision(rows);
    expect(grouped.map((g) => g.revision)).toEqual([3, 1]);
    expect(grouped[1].rows.map((r) => r.party)).toEqual(["OEM", "FACTORY"]);
  });

  it("finds only a VALID acceptance for a side", () => {
    const rows = [acceptance({ party: "OEM", revision: 1, valid: false }),
                  acceptance({ party: "FACTORY", revision: 3, valid: true })];
    expect(currentAcceptance(rows, "OEM")).toBeNull();
    expect(currentAcceptance(rows, "FACTORY")?.revision).toBe(3);
  });
});

describe("acceptBlockers: Accept is disabled, with the reason, when the server would refuse", () => {
  it("allows a clean, unaccepted statement", () => {
    expect(acceptBlockers(statement(), "OEM")).toEqual([]);
  });

  it("blocks a final statement", () => {
    expect(acceptBlockers(statement({ agreed: true }), "OEM")[0]).toMatch(/final/);
  });

  it("blocks a purged statement", () => {
    expect(acceptBlockers(statement({ content: null, content_purged: true }), "OEM")[0])
      .toMatch(/removed/);
  });

  it("blocks a side that already accepted this revision, not the other side", () => {
    const s = statement({ acceptances: [acceptance({ party: "FACTORY", revision: 3, valid: true })] });
    expect(acceptBlockers(s, "FACTORY")[0]).toMatch(/already accepted revision 3/);
    expect(acceptBlockers(s, "OEM")).toEqual([]);
  });

  it("blocks while disputes are live, and counts them", () => {
    const live = (id: number, status: string) => ({
      id, contract_id: 1, statement_id: 5, installation_id: 12,
      window_start: "2026-05-02T00:00:00Z", window_end: "2026-05-02T01:00:00Z",
      raised_by_party: "FACTORY" as const, raised_at: "2026-06-02T00:00:00Z", reason: "x",
      proposed_bucket: "FACTORY", status, resolution_bucket: null, resolution_note: null,
      resolution_proposed_by_party: null, resolution_proposed_at: null,
      resolution_accepted_at: null, closed_at: null });
    const s = statement({ disputes: [live(1, "open"), live(2, "resolution_proposed"),
                                     live(3, "withdrawn"), live(4, "resolved")] });
    expect(liveDisputes(s.disputes).map((d) => d.id)).toEqual([1, 2]);
    expect(acceptBlockers(s, "OEM")).toEqual(
      ["2 open disputes must be resolved or withdrawn first"]);
    expect(acceptBlockers(statement({ disputes: [live(1, "open")] }), "OEM")).toEqual(
      ["1 open dispute must be resolved or withdrawn first"]);
  });

  it("blocks time the rules made DISPUTED until a dispute settles it", () => {
    const base = statement();
    const s = statement({ content: { ...base.content!, sla: { ...base.content!.sla,
                                                             state: "pending_disputes" } } });
    expect(acceptBlockers(s, "OEM")[0]).toMatch(/raise a dispute over that time/);
  });
});

// Produced by the BACKEND: canonical.canonical_bytes() of
// {"bucket": "FACTORY", "reason": "Ölpumpe ausgefallen — नो मटेरियल", "seconds": 3600}
// and canonical.content_hash() of the same object.
const CANONICAL = "{\"bucket\":\"FACTORY\",\"reason\":\"Ölpumpe ausgefallen — "
  + "नो मटेरियल\",\"seconds\":3600}";
const SERVER_HASH = "3ef65c41cac109befb92d5ad6ab272074b1c06b0e78fb1958ba42bac66e69aa3";

describe("verifyCanonical: check the exact downloaded bytes", () => {
  it("matches the server's hash for canonical bytes with non-ASCII text", async () => {
    const bytes = new TextEncoder().encode(CANONICAL);
    expect(bytes.byteLength).toBe(100);
    await expect(verifyCanonical(bytes, SERVER_HASH)).resolves.toEqual(
      { hash: SERVER_HASH, matches: true });
    await expect(verifyCanonical(bytes.buffer.slice(0), SERVER_HASH)).resolves.toMatchObject(
      { matches: true });
  });

  it("does not match a re-serialisation of the same content", async () => {
    // Pretty-printed or key-reordered JSON is the same data and DIFFERENT bytes:
    // the check is over what was stored and accepted, never a re-serialisation.
    const pretty = new TextEncoder().encode(JSON.stringify(JSON.parse(CANONICAL), null, 1));
    const result = await verifyCanonical(pretty, SERVER_HASH);
    expect(result.matches).toBe(false);
    expect(result.hash).toMatch(/^[0-9a-f]{64}$/);
  });

  it("compares the whole lowercase hex digest, not a prefix", async () => {
    const bytes = new TextEncoder().encode(CANONICAL);
    expect((await verifyCanonical(bytes, SERVER_HASH.slice(0, 12))).matches).toBe(false);
    expect((await verifyCanonical(bytes, SERVER_HASH.toUpperCase())).matches).toBe(false);
  });
});

describe("fetchStatementBytes", () => {
  it("returns the raw bytes with the hash and revision headers", async () => {
    const body = new TextEncoder().encode(CANONICAL);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true, status: 200,
      headers: new Headers({ "X-Content-SHA256": SERVER_HASH, "X-Statement-Revision": "3" }),
      arrayBuffer: async () => body.buffer.slice(0),
    } as unknown as Response);
    const got = await fetchStatementBytes("/oem/contracts/1/statements/5/download");
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/oem\/contracts\/1\/statements\/5\/download$/);
    expect(got.hash).toBe(SERVER_HASH);
    expect(got.revision).toBe("3");
    expect(Array.from(new Uint8Array(got.bytes))).toEqual(Array.from(body));
  });

  it("throws a refusal that keeps the status and the detail", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: false, status: 410, headers: new Headers(),
      text: async () => JSON.stringify({ detail: "The content of this statement was removed" }),
    } as unknown as Response);
    const e = await fetchStatementBytes("/service-contracts/1/statements/5/download").catch((x) => x);
    expect(contractError(e)).toMatchObject({ status: 410,
                                             message: "The content of this statement was removed" });
  });
});

describe("contractError reads every shape a refusal arrives in", () => {
  it("an OEM refusal, including consent withheld", () => {
    const withheld = new OemRequestError(403, "withheld until it does",
      { withheld: true, reason: "sharing withdrawn by factory", message: "withheld until it does" });
    expect(contractError(withheld)).toEqual({ status: 403, message: "withheld until it does",
                                              withheld: true, detail: withheld.detail });
  });

  it("a factory GET refusal (apiGet's 'Failed request: path | status | body')", () => {
    const e = new Error('Failed request: /service-contracts/9 | 404 | {"detail":"Contract not found"}');
    expect(contractError(e)).toMatchObject({ status: 404, message: "Contract not found",
                                             withheld: false });
  });

  it("a factory POST refusal (apiPost's raw body) with a structured detail", () => {
    const e = new Error(JSON.stringify({ detail: { field: "grant_downtime_sharing",
                                                   message: "needs grant_downtime_sharing: true" } }));
    expect(contractError(e)).toMatchObject({
      status: null, message: "grant_downtime_sharing: needs grant_downtime_sharing: true" });
  });

  it("anything else keeps its own message", () => {
    expect(contractError(new Error("network down")).message).toBe("network down");
    expect(contractError("boom").message).toBe("boom");
    expect(contractError(new Error('Failed request: /x | 500 | <html>')).message)
      .toBe("<html>");
  });
});

describe("time inputs", () => {
  it("knows a period has closed only once its end has passed", () => {
    const end = "2026-06-01T00:00:00Z";
    expect(closedPeriod({ end }, Date.parse(end) - 1)).toBe(false);
    expect(closedPeriod({ end }, Date.parse(end))).toBe(true);
  });

  it("turns a typed UTC instant into the API's whole-second form, or refuses it", () => {
    expect(parseUtcInput("2026-05-02T08:30")).toBe("2026-05-02T08:30:00Z");
    expect(parseUtcInput("2026-05-02T08:30:15")).toBe("2026-05-02T08:30:15Z");
    expect(parseUtcInput("2026-05-02T08:30:15Z")).toBe("2026-05-02T08:30:15Z");
    for (const bad of ["", "2026-05-02", "2026-13-02T08:30", "2026-02-30T08:30",
                       "2026-05-02T25:00", "02/05/2026 08:30", "2026-05-02T08:30:15.5Z"]) {
      expect(parseUtcInput(bad), bad).toBeNull();
    }
  });
});

describe("termsTemplate", () => {
  it("starts from the plan's conservative defaults and the chosen machines", () => {
    const t = termsTemplate([{ installation_id: 12, serial_number: "AER-0042" }]);
    expect(t.trusted_sources).toEqual(["mqtt"]);
    expect(Object.keys(t.status_defaults).sort()).toEqual(["Breakdown", "Maintenance", "Offline"]);
    expect(t.generic_reasons).toEqual(["breakdown", "unknown"]);
    expect(t.covered_installations).toEqual([{ installation_id: 12, serial_number: "AER-0042" }]);
    expect(t.period_fee).toMatch(/^\d+\.\d{2}$/);
    expect(t.currency).toBe("INR");
  });
});
