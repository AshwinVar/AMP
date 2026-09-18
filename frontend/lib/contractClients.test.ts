import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { termsTemplate } from "./contracts";
import { oemContractsApi } from "./oemContracts";
import { serviceContractsApi } from "./serviceContracts";

/**
 * The two transports of one contract (ADR-0021).
 *
 * The manufacturer and the factory reach the same service through different
 * prefixes and different auth. Every operation is pinned here to its method,
 * path and body, because a body that silently drops `revision` or
 * `grant_downtime_sharing` does not fail — the server refuses it, and the screen
 * shows a refusal nobody can explain.
 */

function ok(body: unknown = {}) {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue(ok({}));
  vi.stubGlobal("fetch", fetchMock);
  localStorage.setItem("token", "h.e30=.s");
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

function call(i = 0) {
  const [url, init] = fetchMock.mock.calls[i] as [string, RequestInit | undefined];
  const path = new URL(String(url)).pathname;
  return {
    path,
    method: init?.method ?? "GET",
    body: init?.body ? JSON.parse(String(init.body)) : undefined,
  };
}

const TERMS = termsTemplate([{ installation_id: 12, serial_number: "AER-0042" }]);
const DISPUTE = { installation_id: 12, window_start: "2026-05-02T08:00:00Z",
                  window_end: "2026-05-02T09:00:00Z", reason: "planned stop",
                  proposed_bucket: "FACTORY" };

type Case = [string, () => Promise<unknown>, string, string, unknown];

function shared(api: typeof oemContractsApi | typeof serviceContractsApi, prefix: string): Case[] {
  return [
    ["list", () => api.list(), "GET", prefix, undefined],
    ["detail", () => api.detail(7), "GET", `${prefix}/7`, undefined],
    ["periods", () => api.periods(7), "GET", `${prefix}/7/periods`, undefined],
    ["preview", () => api.preview(7), "GET", `${prefix}/7/preview`, undefined],
    ["compute", () => api.compute(7, "2026-04-30T18:30:00Z"), "POST",
     `${prefix}/7/statements/compute`, { period_start: "2026-04-30T18:30:00Z" }],
    ["statement", () => api.statement(7, 5), "GET", `${prefix}/7/statements/5`, undefined],
    ["verify", () => api.verify(7, 5), "GET", `${prefix}/7/statements/5/verify`, undefined],
    ["accept statement", () => api.acceptStatement(7, 5, "c".repeat(64), 3), "POST",
     `${prefix}/7/statements/5/accept`, { content_hash: "c".repeat(64), revision: 3 }],
    ["raise dispute", () => api.raiseDispute(7, 5, DISPUTE), "POST",
     `${prefix}/7/statements/5/disputes`, DISPUTE],
    ["propose resolution", () => api.proposeResolution(7, 2, "FACTORY", "agreed on call"), "POST",
     `${prefix}/7/disputes/2/propose-resolution`,
     { resolution_bucket: "FACTORY", note: "agreed on call" }],
    ["accept resolution", () => api.acceptResolution(7, 2, "FACTORY"), "POST",
     `${prefix}/7/disputes/2/accept-resolution`, { resolution_bucket: "FACTORY" }],
    ["withdraw dispute", () => api.withdrawDispute(7, 2), "POST",
     `${prefix}/7/disputes/2/withdraw`, {}],
    ["history", () => api.history(7), "GET", `${prefix}/7/history`, undefined],
    ["terminate", () => api.terminate(7, "site closing"), "POST", `${prefix}/7/terminate`,
     { reason: "site closing" }],
    ["draft amendment", () => api.draftAmendment(7, TERMS, "2026-08-31T18:30:00Z"), "POST",
     `${prefix}/7/amendments`, { terms: TERMS, effective_from: "2026-08-31T18:30:00Z" }],
    ["propose amendment", () => api.proposeAmendment(7, 2, "h".repeat(64)), "POST",
     `${prefix}/7/amendments/2/propose`, { terms_hash: "h".repeat(64) }],
    ["accept amendment", () => api.acceptAmendment(7, 2, "h".repeat(64)), "POST",
     `${prefix}/7/amendments/2/accept`, { terms_hash: "h".repeat(64) }],
    ["reject amendment", () => api.rejectAmendment(7, 2, "not now"), "POST",
     `${prefix}/7/amendments/2/reject`, { note: "not now" }],
  ];
}

describe.each([
  ["the manufacturer", oemContractsApi, "/oem/contracts", "OEM"],
  ["the factory", serviceContractsApi, "/service-contracts", "FACTORY"],
] as const)("%s's transport", (_who, api, prefix, side) => {
  it("names its side", () => {
    expect(api.side).toBe(side);
    expect(api.downloadPath(7, 5)).toBe(`${prefix}/7/statements/5/download`);
  });

  it.each(shared(api, prefix))("%s", async (_label, run, method, path, body) => {
    await run();
    const c = call();
    expect(c.method).toBe(method);
    expect(c.path).toBe(path);
    expect(c.body).toEqual(body);
  });
});

describe("only the manufacturer drafts, proposes and withdraws", () => {
  it.each([
    ["create", () => oemContractsApi.create({ contract_ref: "AMC-1", title: "AMC",
      contract_type: "AMC", factory_tenant_code: "FACTORY_A", start_month: "2026-05",
      terms: TERMS }), "/oem/contracts",
     { contract_ref: "AMC-1", title: "AMC", contract_type: "AMC",
       factory_tenant_code: "FACTORY_A", start_month: "2026-05", terms: TERMS }],
    ["propose", () => oemContractsApi.propose(7, "h".repeat(64)), "/oem/contracts/7/propose",
     { terms_hash: "h".repeat(64) }],
    ["withdraw", () => oemContractsApi.withdraw(7), "/oem/contracts/7/withdraw", {}],
  ] as const)("%s", async (_label, run, path, body) => {
    await run();
    expect(call()).toEqual({ method: "POST", path, body });
  });

  it("edits a draft with a PUT that replaces it whole", async () => {
    const body = { contract_ref: "AMC-1", title: "AMC", contract_type: "AMC",
                   factory_tenant_code: "FACTORY_A", start_month: "2026-05", terms: TERMS };
    await oemContractsApi.editDraft(7, body);
    expect(call()).toEqual({ method: "PUT", path: "/oem/contracts/7/draft", body });
  });
});

describe("only the factory accepts, rejects and reads its reason vocabulary", () => {
  it("accept carries the terms hash AND the consent flag it was given", async () => {
    await serviceContractsApi.accept(7, "h".repeat(64), true);
    expect(call()).toEqual({ method: "POST", path: "/service-contracts/7/accept",
                             body: { terms_hash: "h".repeat(64), grant_downtime_sharing: true } });
  });

  it("rejects with a note, and reads the vocabulary", async () => {
    await serviceContractsApi.reject(7, "price");
    expect(call(0)).toEqual({ method: "POST", path: "/service-contracts/7/reject",
                              body: { note: "price" } });
    await serviceContractsApi.reasonVocabulary(7);
    expect(call(1)).toMatchObject({ method: "GET",
                                    path: "/service-contracts/7/reason-vocabulary" });
  });
});
