/**
 * Where the e2e run points its browser, and where the app under test believes
 * its API lives.
 *
 * Two facts about this codebase drive everything in this file.
 *
 * 1. `lib/api.ts` reads `NEXT_PUBLIC_API_URL` and falls back to
 *    `http://127.0.0.1:8000`. That value is inlined at build/compile time, so
 *    the specs cannot change it from inside the browser — the server has to be
 *    started with it.
 *
 * 2. The default API base is a DIFFERENT ORIGIN from the app (:8000 vs :3100).
 *    Cross-origin fetches carrying `Authorization` and `Content-Type:
 *    application/json` trigger a CORS preflight, and a preflight that is not
 *    answered fails the request before any mock can matter. So in mocked runs
 *    we point the app at a same-origin path (`/__e2e-api`) that no Next route
 *    serves — the browser treats it as a same-origin fetch (no preflight) and
 *    `page.route()` answers it. `mock-api.ts` still intercepts the :8000
 *    default as a fallback, for the case where someone attaches the suite to a
 *    server that was started without our env.
 */

/** Run against a real backend instead of the route mocks. */
export const LIVE_API = process.env.E2E_LIVE_API === "1";

/**
 * Not 3000 on purpose: the suite must not silently attach to a dev server the
 * developer already has running, because that server was started without the
 * NEXT_PUBLIC_API_URL below and would talk to a real (or absent) backend.
 */
export const PORT = Number(process.env.E2E_PORT ?? 3100);

export const BASE_URL = process.env.E2E_BASE_URL ?? `http://127.0.0.1:${PORT}`;

/** Same-origin, deliberately un-routable by Next: only page.route() answers it. */
export const MOCK_API_BASE = `${BASE_URL}/__e2e-api`;

/** The compiled-in default in lib/api.ts. Intercepted as well — see the note above. */
export const DEFAULT_API_BASE = "http://127.0.0.1:8000";

/** What the app is told to call. */
export const API_BASE = LIVE_API
  ? process.env.NEXT_PUBLIC_API_URL ?? DEFAULT_API_BASE
  : MOCK_API_BASE;

/**
 * Live-run credentials. There is no default password: a suite that logs into a
 * real deployment must be told explicitly who it is, and specs skip themselves
 * when it is missing rather than guessing.
 */
export const LIVE_USER = process.env.E2E_USER ?? "";
export const LIVE_PASSWORD = process.env.E2E_PASSWORD ?? "";
