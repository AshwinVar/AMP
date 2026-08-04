/**
 * Minting and planting the session the dashboard reads on boot.
 *
 * The dashboard does not ask the server who you are. It decodes the JWT in
 * `localStorage` itself (`lib/api.ts getUserRole`, `page.tsx getUserTenant` /
 * `getUserName`) and gates the entire nav off the `role` and `tenant` claims.
 * So role-gating and default-view behaviour can be driven end to end simply by
 * planting a token — no login round-trip, no backend, and no coupling to how
 * the backend happens to sign things today.
 *
 * The signature is a fixed placeholder string. Nothing in the browser verifies
 * it, and in mocked runs nothing on the wire does either: this is test data,
 * not a credential.
 */

import type { Page } from "@playwright/test";

export type SessionSeed = {
  username?: string;
  role?: string;
  /** The token's tenant claim. "DEFAULT" is the founder workspace (isFounder). */
  tenant?: string;
  /** The tenant currently being previewed; the dashboard reads this separately. */
  company?: string;
  /** Seconds from now until `exp`. Negative mints an already-expired token. */
  ttlSeconds?: number;
};

/**
 * Standard base64, NOT base64url. The app decodes claims with `atob()`, which
 * rejects the '-' and '_' of the url alphabet — a base64url token would throw
 * inside `getUserRole` and the whole session would read as role "".
 */
function segment(value: object): string {
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64");
}

export function mintToken(seed: SessionSeed = {}): string {
  const {
    username = "alice",
    role = "Admin",
    tenant = "DEFAULT",
    ttlSeconds = 60 * 60,
  } = seed;

  const now = Math.floor(Date.now() / 1000);
  return [
    segment({ alg: "HS256", typ: "JWT" }),
    segment({ sub: username, role, tenant, exp: now + ttlSeconds }),
    "e2e-signature-never-verified",
  ].join(".");
}

/**
 * Plant the session for the app's origin.
 *
 * Deliberately NOT `page.addInitScript`: an init script re-runs on every
 * navigation, so it would silently re-plant the token on the page you land on
 * after logging out — and the logout spec would then be asserting against its
 * own fixture rather than against the app. Loading /login once and writing
 * localStorage there costs one cheap navigation and keeps the store honest.
 */
export async function seedSession(page: Page, seed: SessionSeed = {}): Promise<string> {
  const token = mintToken(seed);
  const entries: [string, string][] = [
    ["token", token],
    ["username", seed.username ?? "alice"],
    ["role", seed.role ?? "Admin"],
    ["company", seed.company ?? seed.tenant ?? "DEFAULT"],
    // The offline licence fallback, used only if GET /tenant-config fails.
    // "enterprise" keeps every pack unlocked so a gating assertion that fails
    // is a role decision, never a licence accident.
    ["plan", "enterprise"],
  ];

  await page.goto("/login");
  await page.evaluate((pairs) => {
    localStorage.clear();
    for (const [key, value] of pairs) localStorage.setItem(key, value);
  }, entries);

  return token;
}
