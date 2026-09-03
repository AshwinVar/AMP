/**
 * Shared configuration for the k6 load scripts.
 *
 * WHY THIS FILE EXISTS
 * The "265ms to 13ms" numbers in the commit history came from ad-hoc timings
 * that nobody can reproduce. The thing that actually generates load in
 * production — an open dashboard tab issuing 46 requests every three seconds —
 * HAS since been measured, but by `backend/loadtest.py`, a Python driver whose
 * own overhead is 7-12 ms per request. k6 is a real load generator and would
 * measure the server rather than the client; that is what these scripts are
 * for, and they have still never been run. See docs/PERFORMANCE.md. Scripts that
 * each roll their own base URL, their own login and their own header assembly
 * drift apart within a week, and then the numbers stop being comparable. So
 * every decision that must be identical across runs lives here: where the load
 * goes, who it goes as, and how a request is shaped so k6 can aggregate it.
 *
 * The three things this file gets right that a hand-rolled script gets wrong:
 *   1. It refuses to point at anything but localhost unless told twice.
 *   2. It logs in ONCE per test, because /login is rate limited (see login()).
 *   3. It tags every request with its path, because the API's cache-buster
 *      makes every URL unique and k6 would otherwise report 46 x N distinct
 *      URLs instead of 46 endpoints (see req()).
 */
import exec from "k6/execution";
import http from "k6/http";

// ─────────────────────────────────────────────────────────────────────────────
//   *** THIS DEFAULTS TO LOCALHOST, DELIBERATELY AND PERMANENTLY. ***
//
//   Do NOT change the default to a staging or production URL. AMP runs a
//   SINGLE uvicorn worker against a small connection pool with no cache in
//   front of it (backend/Procfile, backend/database.py). A dashboard-poll run
//   at even a modest VU count is a denial-of-service against a real tenant's
//   plant floor, not a benchmark. Operators watch these screens during a shift.
//
//   Pointing at a non-local host is possible, but you have to say so out loud:
//       BASE_URL=https://... ALLOW_REMOTE=1 k6 run load/smoke.js
//   Anything else aborts the run before the first request. See
//   assertTargetIsSafe() below.
// ─────────────────────────────────────────────────────────────────────────────
// Matches the frontend default in frontend/lib/api.ts (API_URL), so a local run
// hits exactly the origin a developer's browser would.
export const DEFAULT_BASE_URL = "http://127.0.0.1:8000";

export const BASE_URL = (__ENV.BASE_URL || DEFAULT_BASE_URL).replace(/\/+$/, "");

// Founder-workspace tenant preview. The dashboard sends X-Tenant when the
// company switcher is off DEFAULT (frontend/lib/api.ts getPreviewTenant); the
// backend honours it only for DEFAULT-claim tokens. Leave unset for a normal run
// — setting it changes which tenant's data volume you are measuring, which is
// usually the point when you set it.
export const TENANT = __ENV.TENANT || "";

const LOCAL_HOSTS = [
  "127.0.0.1",
  "localhost",
  "0.0.0.0",
  "::1",
  "host.docker.internal",
];

/** The host of a URL, without userinfo or port. Deliberately hand-rolled: k6's
 *  runtime has no WHATWG URL global, and the guard below must not depend on one. */
function hostOf(rawUrl) {
  const match = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\/([^/?#]+)/.exec(rawUrl);
  if (!match) return "";
  let authority = match[1];
  const at = authority.lastIndexOf("@");
  if (at !== -1) authority = authority.slice(at + 1); // drop user:pass@
  if (authority.charAt(0) === "[") {
    // IPv6 literal: [::1]:8000
    const close = authority.indexOf("]");
    return close === -1 ? authority : authority.slice(1, close);
  }
  const colon = authority.indexOf(":");
  return (colon === -1 ? authority : authority.slice(0, colon)).toLowerCase();
}

export function isLocalTarget(rawUrl) {
  return LOCAL_HOSTS.indexOf(hostOf(rawUrl)) !== -1;
}

/**
 * Abort the whole test unless the target is local or the operator has opted in.
 *
 * A comment alone would not have been enough. The realistic accident is not
 * someone editing the default — it is someone re-running a shell line from
 * their scrollback with BASE_URL still exported from an earlier debugging
 * session. That is why this is an executable guard in setup(), not a warning:
 * exec.test.abort() stops the run before a single VU starts.
 */
export function assertTargetIsSafe() {
  if (isLocalTarget(BASE_URL)) return;
  if (__ENV.ALLOW_REMOTE === "1") {
    console.warn(
      `LOAD TEST POINTED AT A NON-LOCAL HOST: ${BASE_URL}. ` +
        "ALLOW_REMOTE=1 was set, so this is proceeding. If this is a tenant's " +
        "live plant, stop it now.",
    );
    return;
  }
  exec.test.abort(
    `Refusing to load test ${BASE_URL}: it is not localhost. ` +
      "AMP serves a single uvicorn worker with no cache, so this run would be " +
      "an outage for whoever is using that instance. If you genuinely mean it, " +
      "re-run with ALLOW_REMOTE=1.",
  );
}

/**
 * Log in once and hand the token to every VU.
 *
 * This MUST stay a setup()-time, once-per-test call. backend/http_security.py
 * throttles /login to RATE_LIMIT_LOGIN (default 10) requests per 60s per client
 * key, and the client key is the left-most x-forwarded-for hop or the socket
 * peer — every k6 VU on one machine lands in the SAME bucket. A script that
 * logged in per VU would therefore start returning 429 at the 11th VU, and the
 * run would silently be measuring the rate limiter instead of the application.
 * Sharing one bearer token across VUs is also what the real world looks like
 * anyway: N tabs open on one operator's account.
 */
export function login() {
  const username = __ENV.LOAD_USER || "";
  const password = __ENV.LOAD_PASS || "";
  if (!username || !password) {
    exec.test.abort(
      "LOAD_USER and LOAD_PASS must be set. No credentials are baked into this " +
        "repository, not even for local runs. Example: " +
        "LOAD_USER=admin LOAD_PASS=... k6 run load/smoke.js",
    );
  }

  const res = http.post(
    `${BASE_URL}/login`,
    JSON.stringify({ username: username, password: password }),
    { headers: { "Content-Type": "application/json" }, tags: { name: "/login" } },
  );

  if (res.status === 429) {
    exec.test.abort(
      "/login returned 429. The login throttle (10/min per client by default, " +
        "backend/http_security.py RATE_LIMITS) is still holding from an earlier " +
        "run. Wait a minute, or raise RATE_LIMIT_LOGIN on the target instance.",
    );
  }
  if (res.status !== 200) {
    exec.test.abort(
      `/login returned ${res.status} (expected 200). Body: ${String(res.body).slice(0, 200)}`,
    );
  }

  const token = res.json("access_token");
  if (!token) {
    exec.test.abort("/login returned 200 but no access_token in the body.");
  }
  return {
    token: token,
    role: res.json("role"),
    tenant: res.json("tenant"),
  };
}

export function authHeaders(token) {
  const headers = { Authorization: `Bearer ${token}` };
  if (TENANT) headers["X-Tenant"] = TENANT;
  return headers;
}

/**
 * A GET URL with the dashboard's cache-buster.
 *
 * frontend/lib/api.ts appends `?t=<epoch ms>` to every GET and sends
 * cache: "no-store". Reproducing it matters: without it a load test could sit
 * behind a proxy cache the real dashboard defeats on every single request, and
 * report latencies the application never actually achieves.
 */
export function url(path) {
  const separator = path.indexOf("?") !== -1 ? "&" : "?";
  return `${BASE_URL}${path}${separator}t=${Date.now()}`;
}

/**
 * A tagged GET request object, ready for http.batch() or http.request().
 *
 * The `name` tag is the load-bearing part. k6 aggregates http_req_* by URL, and
 * the cache-buster above makes every URL unique — without an explicit name tag
 * a 5-VU, 1-minute dashboard run produces several thousand one-sample URL rows
 * instead of 46 endpoint rows, and the output is unreadable. Tagging by the
 * clean path is also what lets thresholds.js declare per-endpoint budgets.
 */
export function req(path, token, extraTags) {
  const tags = { name: path };
  for (const key in extraTags) tags[key] = extraTags[key];
  return {
    method: "GET",
    url: url(path),
    params: { headers: authHeaders(token), tags: tags },
  };
}

/** Single tagged GET, for the sequential walk in smoke.js. */
export function get(path, token) {
  return http.get(url(path), {
    headers: authHeaders(token),
    tags: { name: path },
  });
}

/** Positive integer from the environment, with a default. Keeps every script
 *  parameterised the same way so runs are comparable across scripts. */
export function envInt(name, fallback) {
  const raw = __ENV[name];
  if (raw === undefined || raw === "") return fallback;
  const parsed = parseInt(raw, 10);
  if (isNaN(parsed) || parsed < 1) return fallback;
  return parsed;
}

export function envStr(name, fallback) {
  const raw = __ENV[name];
  return raw === undefined || raw === "" ? fallback : raw;
}

/** One banner per run so a pasted result always says what it was measuring.
 *  A latency number without its VU count and target is not evidence. */
export function announce(scriptName, extra) {
  const bits = [
    `script=${scriptName}`,
    `target=${BASE_URL}`,
    TENANT ? `tenant=${TENANT}` : "tenant=<token default>",
  ];
  for (const key in extra) bits.push(`${key}=${extra[key]}`);
  console.log(bits.join("  "));
}
