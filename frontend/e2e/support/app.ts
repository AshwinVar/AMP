/**
 * The handful of app-shaped moves every spec needs, in one place.
 *
 * Two reasons this exists rather than being repeated per spec. The obvious one
 * is duplication. The less obvious one is that "open the dashboard" is a
 * three-part manoeuvre in this app — install the API mocks, put a session in
 * localStorage, THEN navigate — and getting the order wrong produces a spec
 * that redirects to /login for reasons that look like a product bug.
 */

import { expect, type Locator, type Page } from "@playwright/test";

import { LIVE_API, LIVE_PASSWORD, LIVE_USER } from "./env";
import { installMockApi, MOCK_PASSWORD, MOCK_USERNAME, type MockTable } from "./mock-api";
import { seedSession, type SessionSeed } from "./session";

/** The sidebar. One <nav> on the page, rendered from the module manifest. */
export function nav(page: Page): Locator {
  return page.getByRole("navigation");
}

/** The title in the topbar, which tracks the active view. */
export function activeViewTitle(page: Page): Locator {
  return page.locator("header h1");
}

function escapeForRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * The value of a KPI tile, given its label.
 *
 * Every tile in this app is `<p>label</p>` followed by a heading holding the
 * value (`KpiCard` in the dashboard, `Kpi` in Mission Control and Machine
 * Health), so the value is the label's next sibling. Matching on `p`
 * specifically matters: severity badges elsewhere on Mission Control are
 * `span`s with the same words ("Critical", "High") and would otherwise make
 * the locator ambiguous.
 */
export function kpiValue(root: Locator, label: string): Locator {
  return root
    .locator("p")
    .filter({ hasText: new RegExp(`^${escapeForRegExp(label)}$`) })
    .locator("xpath=following-sibling::*[1]");
}

/** Drive the real login form. Used by the auth spec, and by every live run. */
export async function signIn(
  page: Page,
  credentials: { username: string; password: string },
): Promise<void> {
  await page.goto("/login");
  await page.getByPlaceholder("Username", { exact: true }).fill(credentials.username);
  await page.getByPlaceholder("Password", { exact: true }).fill(credentials.password);
  await page.getByRole("button", { name: "Login" }).click();
}

/**
 * Get to a loaded dashboard as `session`, with `api` layered over the default
 * fixtures.
 *
 * In a live run the session seed is ignored — you are whoever E2E_USER is — so
 * any spec that depends on a particular role must skip itself there.
 */
export async function openDashboard(
  page: Page,
  options: { session?: SessionSeed; api?: MockTable } = {},
): Promise<void> {
  await installMockApi(page, options.api);

  if (LIVE_API) {
    if (!LIVE_USER || !LIVE_PASSWORD) {
      throw new Error("E2E_LIVE_API=1 needs E2E_USER and E2E_PASSWORD to be set");
    }
    await signIn(page, { username: LIVE_USER, password: LIVE_PASSWORD });
    await page.waitForURL("**/dashboard");
  } else {
    await seedSession(page, options.session);
  }

  await page.goto("/dashboard");

  // The nav is the first thing that proves we are past the auth guard and the
  // module manifest has been applied; every spec builds on it.
  await expect(nav(page)).toBeVisible();
}

export { MOCK_PASSWORD, MOCK_USERNAME };
