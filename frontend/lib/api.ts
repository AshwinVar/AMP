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

  return res.json();
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify(body),
    cache: "no-store",
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Failed request: ${path}`);
  }

  return res.json();
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    method: "PUT",
    headers: getAuthHeaders(),
    body: JSON.stringify(body),
    cache: "no-store",
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Failed request: ${path}`);
  }

  return res.json();
}

export async function apiPatch<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    method: "PATCH",
    headers: getAuthHeaders(),
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed request: ${path} | ${res.status} | ${text}`);
  }

  return res.json();
}

export async function apiDelete(path: string): Promise<void> {
  const res = await fetch(`${API_URL}${path}`, {
    method: "DELETE",
    headers: getAuthHeaders(),
    cache: "no-store",
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed request: ${path} | ${res.status} | ${text}`);
  }
}