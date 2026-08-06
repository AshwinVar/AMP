export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export function getToken() {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("token") || "";
}

export function getUserRole(): string {
  const token = getToken();
  if (!token) return "";
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return payload.role || "";
  } catch {
    return "";
  }
}

// Founder company-switcher preview: when the platform workspace has switched
// to a customer tenant (localStorage "company"), every request carries an
// X-Tenant header. The backend honours it only for DEFAULT-claim tokens
// (tenancy.effective_tenant) — for everyone else it's inert.
function getPreviewTenant(): string {
  if (typeof window === "undefined") return "";
  const company = localStorage.getItem("company") || "";
  return company && company !== "DEFAULT" ? company : "";
}

export function getAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${getToken()}`,
  };
  const preview = getPreviewTenant();
  if (preview) headers["X-Tenant"] = preview;
  return headers;
}

export function getDownloadHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${getToken()}`,
  };
  const preview = getPreviewTenant();
  if (preview) headers["X-Tenant"] = preview;
  return headers;
}

// ── Sliding session ─────────────────────────────────────────────
// When the token is within REFRESH_WINDOW of expiry (but still valid), exchange
// it for a fresh one in the background, so an active user is never logged out
// mid-shift. Throttled so the check costs at most one request per interval;
// idle sessions still expire naturally.
const REFRESH_WINDOW_MS = 60 * 60 * 1000;   // refresh when < 60 min of life left
const REFRESH_THROTTLE_MS = 5 * 60 * 1000;  // attempt at most every 5 min
let lastRefreshAttempt = 0;

function maybeRefreshToken() {
  if (typeof window === "undefined") return;
  const now = Date.now();
  if (now - lastRefreshAttempt < REFRESH_THROTTLE_MS) return;
  const token = getToken();
  if (!token) return;
  let expMs = 0;
  try {
    expMs = (JSON.parse(atob(token.split(".")[1])).exp || 0) * 1000;
  } catch {
    return;
  }
  if (expMs <= now || expMs - now > REFRESH_WINDOW_MS) return;  // expired or still fresh
  lastRefreshAttempt = now;
  fetch(`${API_URL}/auth/refresh`, { method: "POST", headers: getAuthHeaders() })
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      if (d?.access_token) localStorage.setItem("token", d.access_token);
    })
    .catch(() => {
      // best-effort — the next call will retry after the throttle window.
    });
}

// ── Session-expiry handling ─────────────────────────────────────
// When an authenticated call comes back 401 and the stored token is genuinely
// expired, the session is over: clear it and send the user to /login instead of
// leaving a silently broken dashboard. Guarded so a single flake can't loop.
let redirectingToLogin = false;

function handleUnauthorized() {
  if (typeof window === "undefined" || redirectingToLogin) return;
  if (window.location.pathname.startsWith("/login")) return;
  const token = getToken();
  if (!token) return;
  let expMs = 0;
  try {
    expMs = (JSON.parse(atob(token.split(".")[1])).exp || 0) * 1000;
  } catch {
    expMs = 0;  // unreadable token -> treat as dead
  }
  if (expMs > Date.now()) return;  // still valid — the 401 is something else, don't log out
  redirectingToLogin = true;
  localStorage.removeItem("token");
  window.location.href = "/login";
}

/**
 * Read a SUCCESSFUL response's body, tolerating a 2xx that does not have one.
 *
 * `res.json()` throws a SyntaxError on an empty body, so a 204 No Content — or
 * any 2xx that arrives empty — made a committed write look like a failed one.
 * The operator then retries, and on a non-idempotent endpoint the retry is a
 * duplicate: the worst outcome available, from a request that worked.
 *
 * Reading as text and deciding is deliberate rather than sniffing `res.status`
 * for 204. A gateway, a proxy, or a handler returning "" all produce a 2xx with
 * nothing to parse, and a status check would still throw on those.
 *
 * ON THE `as T`. This lies to the type system: the signature promises T and
 * this can hand back null. The honest alternative is `Promise<T | null>`, which
 * would force a null check at every one of the dozens of call sites that
 * destructure the result — to describe a case that only arises on endpoints
 * returning no body, where the caller is not reading the result anyway. The
 * unsoundness is contained to those endpoints and is the lesser cost, but it IS
 * one, so it is written down rather than hidden.
 */
async function readBody<T>(res: Response): Promise<T> {
  const text = await res.text();
  if (!text) return null as T;
  return JSON.parse(text) as T;
}

export async function apiGet<T>(path: string): Promise<T> {
  maybeRefreshToken();
  const sep = path.includes("?") ? "&" : "?";
  const res = await fetch(`${API_URL}${path}${sep}t=${Date.now()}`, {
    method: "GET",
    headers: getAuthHeaders(),
    cache: "no-store",
  });

  if (!res.ok) {
    if (res.status === 401) handleUnauthorized();
    const text = await res.text();
    throw new Error(`Failed request: ${path} | ${res.status} | ${text}`);
  }

  // Deliberately NOT readBody(). Left as res.json() because every GET in this
  // API returns data by definition — a read with nothing to read is not a case
  // this backend has — and this is the hot path: the dashboard issues 46 of
  // these every 3 seconds per open tab. Changing it would be a behaviour change
  // on the busiest code in the app to cover a case that does not exist. If a
  // GET ever legitimately 204s, move it to readBody and add a test alongside
  // the write ones.
  return res.json();
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  maybeRefreshToken();
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify(body),
    cache: "no-store",
  });

  if (!res.ok) {
    if (res.status === 401) handleUnauthorized();
    // The raw body is surfaced deliberately: CsvImportButton reads `detail`
    // from it to show the backend's actionable 400 (#371).
    const text = await res.text();
    throw new Error(text || `Failed request: ${path}`);
  }

  return readBody<T>(res);
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  maybeRefreshToken();
  const res = await fetch(`${API_URL}${path}`, {
    method: "PUT",
    headers: getAuthHeaders(),
    body: JSON.stringify(body),
    cache: "no-store",
  });

  if (!res.ok) {
    if (res.status === 401) handleUnauthorized();
    const text = await res.text();
    throw new Error(text || `Failed request: ${path}`);
  }

  return readBody<T>(res);
}

export async function apiPatch<T>(path: string, body?: unknown): Promise<T> {
  maybeRefreshToken();
  const res = await fetch(`${API_URL}${path}`, {
    method: "PATCH",
    headers: getAuthHeaders(),
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });

  if (!res.ok) {
    if (res.status === 401) handleUnauthorized();
    const text = await res.text();
    throw new Error(`Failed request: ${path} | ${res.status} | ${text}`);
  }

  return readBody<T>(res);
}

export async function apiDelete(path: string): Promise<void> {
  maybeRefreshToken();
  const res = await fetch(`${API_URL}${path}`, {
    method: "DELETE",
    headers: getAuthHeaders(),
    cache: "no-store",
  });

  if (!res.ok) {
    if (res.status === 401) handleUnauthorized();
    const text = await res.text();
    throw new Error(`Failed request: ${path} | ${res.status} | ${text}`);
  }
}