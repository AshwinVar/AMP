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

export function getAuthHeaders() {
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${getToken()}`,
  };
}

export function getDownloadHeaders() {
  return {
    Authorization: `Bearer ${getToken()}`,
  };
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

export async function apiGet<T>(path: string): Promise<T> {
  maybeRefreshToken();
  const sep = path.includes("?") ? "&" : "?";
  const res = await fetch(`${API_URL}${path}${sep}t=${Date.now()}`, {
    method: "GET",
    headers: getAuthHeaders(),
    cache: "no-store",
  });

  if (!res.ok) {
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