import { expect, test } from "@playwright/test";

import { openDashboard, signIn } from "./support/app";
import { LIVE_API, LIVE_PASSWORD, LIVE_USER } from "./support/env";
import { installMockApi, MOCK_PASSWORD, MOCK_USERNAME, respondWith } from "./support/mock-api";
import { seedSession } from "./support/session";

/**
 * The front door.
 *
 * Everything else in the product is behind it, and none of it was tested. The
 * session in this app is four localStorage keys and a JWT the client decodes
 * itself; there is no server-rendered auth, no cookie, no middleware. That
 * makes four things worth pinning, because each is a different code path that
 * has already been wrong once:
 *
 *   - the login form stores what the dashboard later reads (app/login/page.tsx)
 *   - a rejected login surfaces the backend's own reason, not a generic string
 *   - arriving with no token at all bounces to /login (dashboard boot guard)
 *   - a token that has EXPIRED ends the session on the next 401, while a stray
 *     401 on a live token does not (lib/api.ts handleUnauthorized, #393)
 *
 * That last pair is why this file has a CONTROL case: "we ended up on /login"
 * is true both for a correct expiry check and for a naive "any 401 logs you
 * out", and the second one throws people off the dashboard mid-shift.
 */

const credentials = LIVE_API
  ? { username: LIVE_USER, password: LIVE_PASSWORD }
  : { username: MOCK_USERNAME, password: MOCK_PASSWORD };

test.describe("authentication", () => {
  test("signing in stores the session and opens the dashboard", async ({ page }) => {
    test.skip(LIVE_API && !LIVE_USER, "a live run needs E2E_USER and E2E_PASSWORD");

    await installMockApi(page);
    await signIn(page, credentials);

    await page.waitForURL("**/dashboard");
    await expect(page.getByText(/Welcome back,/)).toBeVisible();

    // The dashboard reads all four of these on boot; storing three of them is
    // a silent half-login that only shows up as a blank nav.
    const session = await page.evaluate(() => ({
      token: localStorage.getItem("token"),
      username: localStorage.getItem("username"),
      role: localStorage.getItem("role"),
      company: localStorage.getItem("company"),
    }));
    expect(session.token ?? "").not.toBe("");
    expect(session.username).toBe(credentials.username);
    expect(session.role ?? "").not.toBe("");
    expect(session.company ?? "").not.toBe("");
  });

  test("a rejected login shows the reason and stores nothing", async ({ page }) => {
    // Skipped live because the backend rate-limits failed logins, so a
    // deliberate wrong password is not a repeatable experiment there.
    test.skip(LIVE_API, "deliberately wrong credentials against a rate-limited backend");

    await installMockApi(page);
    await signIn(page, { username: MOCK_USERNAME, password: "not-the-password" });

    await expect(page.getByText("Invalid username or password")).toBeVisible();
    await expect(page).toHaveURL(/\/login$/);
    expect(await page.evaluate(() => localStorage.getItem("token"))).toBeNull();
  });

  test("arriving at the dashboard with no token bounces to /login", async ({ page }) => {
    await installMockApi(page);

    await page.goto("/dashboard");

    await page.waitForURL("**/login");
    await expect(page.getByRole("button", { name: "Login" })).toBeVisible();
  });

  test("an expired token ends the session on the first 401", async ({ page }) => {
    await installMockApi(page, {
      // A real backend rejects the expired token by itself; the mock has to be
      // told to.
      "*": respondWith(401, { detail: "Token has expired" }),
    });
    await seedSession(page, { ttlSeconds: -60 });

    // Proves the redirect below is the expiry path and not the "no token at
    // all" boot guard, which would pass this test for the wrong reason.
    expect(await page.evaluate(() => localStorage.getItem("token"))).not.toBeNull();

    await page.goto("/dashboard");

    await page.waitForURL("**/login");
    // Not just redirected - the dead token is cleared, so a back button does
    // not drop the user into a half-broken dashboard again.
    expect(await page.evaluate(() => localStorage.getItem("token"))).toBeNull();
  });

  test("CONTROL: a 401 from one endpoint on a live token keeps the user working", async ({ page }) => {
    test.skip(LIVE_API, "needs a forced 401 from a single endpoint");

    // GET /users is fetched once on mount for an Admin. Everything else answers
    // normally, so this is exactly the "stray 401" shape: the session is fine.
    await installMockApi(page, { "/users": respondWith(401, { detail: "Not permitted" }) });
    await seedSession(page, { ttlSeconds: 3600 });
    await page.goto("/dashboard");

    // A fixed wait, because the assertion is that a navigation does NOT happen:
    // 4s covers the mount fetch and a full 3s poll round after it.
    await page.waitForTimeout(4000);

    await expect(page).toHaveURL(/\/dashboard$/);
    expect(await page.evaluate(() => localStorage.getItem("token"))).not.toBeNull();
    await expect(page.getByRole("navigation")).toBeVisible();
  });

  test("logging out clears the session and returns to /login", async ({ page }) => {
    await openDashboard(page);

    await page.getByRole("button", { name: "Logout" }).click();

    await page.waitForURL("**/login");
    const remaining = await page.evaluate(() => ({
      token: localStorage.getItem("token"),
      role: localStorage.getItem("role"),
      keys: localStorage.length,
    }));
    expect(remaining.token).toBeNull();
    expect(remaining.role).toBeNull();
    expect(remaining.keys).toBe(0);
  });
});
