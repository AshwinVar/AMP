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

  it("makes exactly one request when nobody is signed in yet", async () => {
    // The login form's own POST runs through this same path, with an empty
    // token. Signing in must cost one request: anything the sliding session
    // adds here is a `Bearer `-authenticated call that can only 401, on the one
    // screen where a stray 401 in the log is genuinely confusing to debug.
    const fetchMock = respond(401, { detail: "Incorrect username or password" });
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.apiPost("/auth/login", { username: "ashwin" })).rejects.toThrow();

    expect(fetchMock.mock.calls).toHaveLength(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/auth/login");
  });

  it("attempts the refresh once for a burst of requests, not once per request", async () => {
    // The dashboard fires ~47 requests every three seconds. Without the
    // throttle, every one of them inside the refresh window would post to
    // /auth/refresh - fifteen token mints a second, per open tab, all racing to
    // write localStorage.
    localStorage.setItem("token", LIVE_BUT_NEAR);
    const fetchMock = vi.fn().mockImplementation((url: string) =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () =>
          String(url).includes("/auth/refresh") ? { access_token: LIVE_AND_FRESH } : {},
        text: async () => "{}",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all([api.apiGet("/machines"), api.apiGet("/work-orders"), api.apiPost("/downtime-logs", {})]);

    const refreshes = fetchMock.mock.calls.filter(([u]) => String(u).includes("/auth/refresh"));
    // One, not three - and not zero: a token this close to expiry must still
    // slide, or the throttle has silently turned the sliding session off.
    expect(refreshes).toHaveLength(1);
  });
});

/**
 * What the refresh does when it does NOT come back with a token.
 *
 * The sliding refresh runs in the background of a request the user is actually
 * waiting on, so its failure modes are invisible until they log someone out.
 * Anything other than a real `access_token` must leave the session exactly as
 * it was and let the throttle window handle the retry.
 */
describe("a refresh that brings back nothing usable", () => {
  const flush = () => new Promise((r) => setTimeout(r, 0));

  const splitFetch = (refresh: () => Promise<unknown>) =>
    vi.fn().mockImplementation((url: string) =>
      String(url).includes("/auth/refresh")
        ? refresh()
        : Promise.resolve({ ok: true, status: 200, json: async () => ({ id: 7 }), text: async () => "{}" }),
    );

  it("keeps the current token when the refresh answers without one", async () => {
    // A backend that renames the field, or answers `{}` on a soft failure,
    // would otherwise have `undefined` written into localStorage - the very
    // next request goes out as "Bearer undefined", 401s, and the user is
    // logged out mid-shift by the mechanism meant to keep them signed in.
    localStorage.setItem("token", LIVE_BUT_NEAR);
    vi.stubGlobal(
      "fetch",
      splitFetch(() => Promise.resolve({ ok: true, status: 200, json: async () => ({}), text: async () => "{}" })),
    );

    await expect(api.apiGet("/machines/7")).resolves.toEqual({ id: 7 });
    await flush();

    expect(localStorage.getItem("token")).toBe(LIVE_BUT_NEAR);
  });

  it("keeps the current token when the refresh is refused", async () => {
    localStorage.setItem("token", LIVE_BUT_NEAR);
    vi.stubGlobal(
      "fetch",
      splitFetch(() =>
        Promise.resolve({ ok: false, status: 401, json: async () => ({ detail: "expired" }), text: async () => "" }),
      ),
    );

    await expect(api.apiGet("/machines/7")).resolves.toEqual({ id: 7 });
    await flush();

    expect(localStorage.getItem("token")).toBe(LIVE_BUT_NEAR);
  });

  it("does not take the user's request down with it when the network drops", async () => {
    // Shop-floor wifi drops constantly. The refresh is best-effort background
    // work; an unhandled rejection from it must not surface on the request the
    // operator is waiting on.
    localStorage.setItem("token", LIVE_BUT_NEAR);
    vi.stubGlobal(
      "fetch",
      splitFetch(() => Promise.reject(new TypeError("Failed to fetch"))),
    );

    await expect(api.apiGet("/machines/7")).resolves.toEqual({ id: 7 });
    await flush();

    expect(localStorage.getItem("token")).toBe(LIVE_BUT_NEAR);
  });
});

/**
 * `handleUnauthorized` decides whether a 401 ends the session by reading the
 * token's own `exp`. Two branches of that decision were never exercised: a
 * token it cannot decode at all, and a 401 raised on the login page itself.
 */

// Records assignments to location.href so "did not navigate" is distinguishable
// from "navigated to the value it already had".
function trackLocation(pathname: string) {
  const navigations: string[] = [];
  Object.defineProperty(window, "location", {
    value: {
      pathname,
      get href() {
        return navigations[navigations.length - 1] ?? pathname;
      },
      set href(value: string) {
        navigations.push(value);
      },
    },
    writable: true,
    configurable: true,
  });
  return navigations;
}

describe("session expiry edge cases", () => {
  it("treats a token it cannot decode as dead", async () => {
    // localStorage can hold a truncated or hand-edited token, and a token whose
    // expiry cannot be read is not evidence of a live session. Treating it as
    // live instead pins the user to a dashboard that 401s forever with no way
    // out except clearing site data - the failure #393 set out to remove.
    const navigations = trackLocation("/dashboard");
    localStorage.setItem("token", "not.a.jwt");
    vi.stubGlobal("fetch", respond(401, { detail: "Not authenticated" }));

    await expect(api.apiGet("/work-orders")).rejects.toThrow();

    expect(localStorage.getItem("token")).toBeNull();
    expect(navigations).toEqual(["/login"]);
  });

  it("does not bounce a 401 that happens on the login page", async () => {
    // The login form's own POST 401s on a wrong password. Sending /login to
    // /login reloads the page, throws away what the user typed, and reads as
    // the app having eaten the submission.
    const onLogin = trackLocation("/login");
    localStorage.setItem("token", EXPIRED); // stale token from the last session
    vi.stubGlobal("fetch", respond(401, { detail: "Incorrect username or password" }));

    await expect(api.apiPost("/auth/login", { username: "ashwin" })).rejects.toThrow();
    expect(onLogin).toEqual([]);

    // Control: the identical 401 from anywhere else DOES move the user, so the
    // empty list above is the /login guard and not a dead mechanism.
    const elsewhere = trackLocation("/dashboard");
    await expect(api.apiPost("/auth/login", { username: "ashwin" })).rejects.toThrow();
    expect(elsewhere).toEqual(["/login"]);
  });

  it("does not end the session on a server error", async () => {
    // Only 401 ends a session. The token here is already expired, so the one
    // thing keeping this user signed in is the status check - widen it to "any
    // failed response" and a single 500 from one endpoint logs the whole shop
    // floor out mid-shift.
    const navigations = trackLocation("/dashboard");
    localStorage.setItem("token", EXPIRED);
    vi.stubGlobal("fetch", respond(500, { detail: "boom" }));

    await expect(api.apiPatch("/work-orders/1", { x: 1 })).rejects.toThrow(/\| 500 \|/);
    await expect(api.apiDelete("/work-orders/1")).rejects.toThrow(/\| 500 \|/);

    // The 401 cases above are the control: same token, same verbs, they DO
    // clear and redirect.
    expect(navigations).toEqual([]);
    expect(localStorage.getItem("token")).toBe(EXPIRED);
  });

  it("navigates once when a whole poll round comes back 401", async () => {
    // Token dies, the next poll round returns ~47 401s at once. Each one is a
    // candidate to assign location.href; the browser should be asked to leave
    // exactly one time.
    //
    // HONEST NOTE ON WHAT THIS TEST CAN AND CANNOT CATCH, measured rather than
    // assumed. Two independent mechanisms enforce "navigate once":
    //
    //   1. `if (redirectingToLogin) return` — the explicit guard.
    //   2. `localStorage.removeItem("token")` runs before the navigation, so
    //      every later call in the same round hits `if (!token) return` first.
    //      Handlers run synchronously to completion on one thread, so the first
    //      401 clears the token before the second is even examined.
    //
    // Removing each guard in turn and re-running this file gives:
    //
    //   drop `redirectingToLogin` only   -> still PASSES   (mechanism 2 covers it)
    //   drop the token removal only      -> FAILS
    //   drop both                        -> FAILS
    //
    // So `redirectingToLogin` is genuinely redundant for THIS behaviour — no
    // assertion here can distinguish its presence, because mechanism 2 already
    // guarantees the outcome. That is defence in depth working as intended, and
    // the right response is to write it down rather than invent an assertion
    // that pretends to tell them apart. (The flag still earns its place against
    // a future refactor that stops clearing the token synchronously.)
    //
    // The second assertion below is what closes the gap on mechanism 2: without
    // it, dropping the token removal would also have passed silently.
    const navigations = trackLocation("/dashboard");
    localStorage.setItem("token", EXPIRED);
    vi.stubGlobal("fetch", respond(401, { detail: "Not authenticated" }));

    await Promise.allSettled(
      Array.from({ length: 6 }, (_, i) => api.apiGet(`/panel-${i}`)),
    );

    expect(navigations).toEqual(["/login"]);
    // The mechanism above, asserted rather than assumed: the session is gone by
    // the time the round finishes, which is what stops the next round retrying.
    expect(localStorage.getItem("token")).toBeNull();
  });
});

type FetchMock = ReturnType<typeof vi.fn>;
const initOf = (m: FetchMock, i = 0) => (m.mock.calls[i] as unknown[])[1] as RequestInit;
const headersOf = (m: FetchMock, i = 0) => initOf(m, i).headers as Record<string, string>;

describe("request shape", () => {
  beforeEach(() => {
    localStorage.setItem("token", LIVE_AND_FRESH);
  });

  it("joins the cache-buster onto a path that already has a query", async () => {
    // `?line=SMT-1?t=...` makes the cache-buster part of the *filter value*.
    // The backend then sees a line nobody has ever heard of and the operator
    // reads an empty - or unfiltered - chart on a page labelled with a filter.
    const fetchMock = respond(200, {});
    vi.stubGlobal("fetch", fetchMock);

    await api.apiGet("/analytics/oee?line=SMT-1&shift=A");

    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\?line=SMT-1&shift=A&t=\d+$/);
  });

  it("opens the query itself when the path has none", async () => {
    // Control for the case above, and the reason GETs are not served stale by
    // the browser at all.
    const fetchMock = respond(200, {});
    vi.stubGlobal("fetch", fetchMock);

    await api.apiGet("/work-orders");

    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/work-orders\?t=\d+$/);
  });

  it("sends no body at all for a bodiless PATCH", async () => {
    // Several PATCH endpoints are pure verbs - `/grns/7/accept`. Serialising a
    // missing body puts the four characters `undefined` on the wire, which is
    // not JSON, and the acceptance 422s instead of happening.
    const fetchMock = respond(200, {});
    vi.stubGlobal("fetch", fetchMock);

    await api.apiPatch("/grns/7/accept");
    expect(initOf(fetchMock, 0).body).toBeUndefined();

    await api.apiPatch("/grns/7/accept", { note: "short delivery" });
    expect(initOf(fetchMock, 1).body).toBe('{"note":"short delivery"}');
  });
});

/**
 * The founder company-switcher. When the platform workspace is previewing a
 * customer tenant, X-Tenant has to ride on EVERY request - miss it on one and
 * the preview shows a page of DEFAULT's numbers under the customer's name,
 * which is the worst possible thing to be looking at during a demo.
 */
describe("tenant preview header", () => {
  beforeEach(() => {
    localStorage.setItem("token", LIVE_AND_FRESH);
  });

  it("stamps X-Tenant on reads, writes and downloads while previewing", async () => {
    localStorage.setItem("company", "ACME");
    const fetchMock = respond(200, {});
    vi.stubGlobal("fetch", fetchMock);

    await api.apiGet("/work-orders");
    await api.apiPost("/work-orders", { x: 1 });

    expect(headersOf(fetchMock, 0)["X-Tenant"]).toBe("ACME");
    expect(headersOf(fetchMock, 1)["X-Tenant"]).toBe("ACME");
    // CSV export/import build their own fetch off these.
    expect(api.getDownloadHeaders()["X-Tenant"]).toBe("ACME");
  });

  it("sends no X-Tenant from the founder's own workspace", async () => {
    // DEFAULT is the platform workspace itself, not a preview of anyone.
    localStorage.setItem("company", "DEFAULT");
    const fetchMock = respond(200, {});
    vi.stubGlobal("fetch", fetchMock);

    await api.apiGet("/work-orders");

    expect(headersOf(fetchMock, 0)).not.toHaveProperty("X-Tenant");
    expect(api.getDownloadHeaders()).not.toHaveProperty("X-Tenant");
  });

  it("leaves Content-Type off the download headers", () => {
    // CsvImportButton posts a FormData with these. A JSON Content-Type would
    // override the multipart boundary the browser writes, and every CSV import
    // would be rejected as malformed (#371).
    expect(api.getDownloadHeaders().Authorization).toBe(`Bearer ${LIVE_AND_FRESH}`);
    expect(api.getDownloadHeaders()).not.toHaveProperty("Content-Type");
    // Control: the JSON path does set it.
    expect(api.getAuthHeaders()["Content-Type"]).toBe("application/json");
  });
});

describe("responses that succeed", () => {
  beforeEach(() => {
    localStorage.setItem("token", LIVE_AND_FRESH);
  });

  it("hands back the parsed body for every verb that returns one", async () => {
    vi.stubGlobal("fetch", respond(200, { id: 7, name: "SMT-1" }));

    await expect(api.apiGet("/machines/7")).resolves.toEqual({ id: 7, name: "SMT-1" });
    await expect(api.apiPost("/machines", {})).resolves.toEqual({ id: 7, name: "SMT-1" });
    await expect(api.apiPut("/machines/7", {})).resolves.toEqual({ id: 7, name: "SMT-1" });
    await expect(api.apiPatch("/machines/7", {})).resolves.toEqual({ id: 7, name: "SMT-1" });
  });

  it("does not try to parse a body on DELETE", async () => {
    // A delete answers with nothing to parse. Reaching for .json() anyway
    // throws on the empty body, so a row that really was deleted reports a
    // failure and the table refuses to refresh.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 204,
        json: async () => {
          throw new SyntaxError("Unexpected end of JSON input");
        },
        text: async () => "",
      }),
    );

    await expect(api.apiDelete("/machines/7")).resolves.toBeUndefined();
  });

  it("still names the failing request when the error body is empty", async () => {
    // A 502 from the proxy carries no body. `new Error("")` is an error nobody
    // can act on: nothing in the console, nothing in the network breadcrumb,
    // no way to tell a dropped gateway from a rejected payload.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 502, json: async () => ({}), text: async () => "" }),
    );

    await expect(api.apiPost("/work-orders", {})).rejects.toThrow(/^Failed request: \/work-orders$/);
    await expect(api.apiPut("/work-orders/1", {})).rejects.toThrow(/^Failed request: \/work-orders\/1$/);
  });
});

describe("getUserRole", () => {
  it("reads the role claim off a good token", () => {
    localStorage.setItem("token", LIVE_AND_FRESH);
    expect(api.getUserRole()).toBe("Admin");
  });

  it("returns no role rather than throwing on a token it cannot decode", () => {
    // Every role-gated surface calls this during render - the nav, the agent
    // policy panel, the briefing. A throw here is a blank white page, not a
    // hidden button.
    localStorage.setItem("token", "not.a.jwt");
    expect(api.getUserRole()).toBe("");
  });

  it("returns no role for a decodable token that carries none", () => {
    localStorage.setItem("token", jwt({ sub: "ashwin", exp: Math.floor(Date.now() / 1000) + 86400 }));
    expect(api.getUserRole()).toBe("");
  });
});
