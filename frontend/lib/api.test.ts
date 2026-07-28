import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Session expiry has to be handled on WRITES too, not just reads.
 *
 * `handleUnauthorized` and `maybeRefreshToken` were wired into `apiGet` only.
 * A user who leaves a tab open past token expiry and then submits a form gets a
 * 401, a thrown error, and no redirect - they stay on a dashboard that looks
 * fine but cannot save anything, and the reason is invisible. Whether they were
 * ever rescued depended on some other component happening to issue a GET.
 *
 * The sliding-session refresh had the same shape: someone working through forms
 * would never slide their session, so it expired sooner than intended.
 */

const jwt = (payload: Record<string, unknown>) => {
  const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64");
  return `${b64({ alg: "HS256" })}.${b64(payload)}.sig`;
};

const EXPIRED = jwt({ sub: "ashwin", role: "Admin", exp: Math.floor(Date.now() / 1000) - 60 });
const LIVE_BUT_NEAR = jwt({ sub: "ashwin", role: "Admin", exp: Math.floor(Date.now() / 1000) + 300 });
const LIVE_AND_FRESH = jwt({ sub: "ashwin", role: "Admin", exp: Math.floor(Date.now() / 1000) + 86400 });

let api: typeof import("./api");

async function freshApi() {
  // The module holds throttle/redirect state, so each test needs its own copy.
  vi.resetModules();
  return import("./api");
}

beforeEach(async () => {
  localStorage.clear();
  Object.defineProperty(window, "location", {
    value: { pathname: "/dashboard", href: "/dashboard" },
    writable: true,
    configurable: true,
  });
  api = await freshApi();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const respond = (status: number, body: unknown = {}) =>
  vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  });

describe("session expiry on writes", () => {
  const writes: [string, (a: typeof api) => Promise<unknown>][] = [
    ["apiPost", (a) => a.apiPost("/work-orders", { x: 1 })],
    ["apiPut", (a) => a.apiPut("/work-orders/1", { x: 1 })],
    ["apiPatch", (a) => a.apiPatch("/work-orders/1", { x: 1 })],
    ["apiDelete", (a) => a.apiDelete("/work-orders/1")],
  ];

  for (const [name, call] of writes) {
    it(`${name} clears the dead session and redirects on 401`, async () => {
      localStorage.setItem("token", EXPIRED);
      vi.stubGlobal("fetch", respond(401, { detail: "Not authenticated" }));

      await expect(call(api)).rejects.toThrow();

      expect(localStorage.getItem("token")).toBeNull();
      expect(window.location.href).toBe("/login");
    });
  }

  it("a 401 on a STILL-VALID token does not log the user out", async () => {
    // A 403-shaped permission problem surfacing as 401 must not nuke the
    // session - that would log people out for clicking something they lack
    // rights to.
    localStorage.setItem("token", LIVE_AND_FRESH);
    vi.stubGlobal("fetch", respond(401, { detail: "nope" }));

    await expect(api.apiPost("/x", {})).rejects.toThrow();

    expect(localStorage.getItem("token")).toBe(LIVE_AND_FRESH);
    expect(window.location.href).toBe("/dashboard");
  });

  it("keeps each verb's existing error message shape", async () => {
    localStorage.setItem("token", LIVE_AND_FRESH);
    vi.stubGlobal("fetch", respond(500, { detail: "boom" }));

    // apiPost surfaces the raw body so CsvImportButton can read `detail` (#371).
    await expect(api.apiPost("/x", {})).rejects.toThrow(/detail/);
    // apiGet/apiPatch/apiDelete keep the diagnostic "path | status | text" form.
    await expect(api.apiGet("/x")).rejects.toThrow(/Failed request: \/x \| 500/);
  });
});

describe("sliding session on writes", () => {
  it("refreshes a near-expiry token when the user is only writing", async () => {
    localStorage.setItem("token", LIVE_BUT_NEAR);
    const fetchMock = vi.fn().mockImplementation((url: string) =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => (String(url).includes("/auth/refresh") ? { access_token: "REFRESHED" } : {}),
        text: async () => "{}",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.apiPost("/downtime-logs", { duration: 5 });
    await vi.waitFor(() => expect(localStorage.getItem("token")).toBe("REFRESHED"));

    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/auth/refresh"))).toBe(true);
  });

  it("does not refresh a token that still has plenty of life", async () => {
    localStorage.setItem("token", LIVE_AND_FRESH);
    const fetchMock = respond(200, {});
    vi.stubGlobal("fetch", fetchMock);

    await api.apiPost("/x", {});

    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/auth/refresh"))).toBe(false);
  });
});
