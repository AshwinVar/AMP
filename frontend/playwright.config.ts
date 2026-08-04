import { defineConfig, devices } from "@playwright/test";

import { BASE_URL, LIVE_API, MOCK_API_BASE, PORT } from "./e2e/support/env";

/**
 * Browser tests for the critical journey.
 *
 * The unit suite (vitest.config.ts) is deliberately scoped to lib/ — logic, no
 * rendering. That leaves 95 components and a 2,933-line dashboard with nothing
 * proving they compose into a working screen: login, the nav, the default view,
 * the machine drill-down and its keyboard behaviour were all only ever verified
 * by a human clicking. This config is the other half.
 *
 * Runs mocked by default (see e2e/support/mock-api.ts), so CI needs a browser
 * and nothing else — no Postgres, no FastAPI, no seeded tenant. E2E_LIVE_API=1
 * points the same specs at a real backend.
 */

/**
 * A production build, not `next dev`, and this is not a preference.
 *
 * Under `next dev` on this project, Turbopack panics every time the browser's
 * HMR channel asks it to rebuild the dashboard route: "Failed to write app
 * endpoint /dashboard/page, caused by: Next.js package not found", with
 * `Execution of Project::hmr_version_state failed` at the top of the trace and
 * its own version reported as 0.0.0. The dev overlay then reloads the page, so
 * clicks land on a document that is about to be replaced, React reports a
 * hydration mismatch, and specs fail in ways that look like product bugs — a
 * logout button that does nothing, a login that never leaves /login. The same
 * six specs take 3.8 minutes of timeouts served by `next dev` and 11.6 seconds
 * served from a build.
 *
 * The build also has to happen with this env applied: NEXT_PUBLIC_API_URL is
 * inlined at build time, so a build without it produces a bundle that ignores
 * the mock base entirely. Keeping build and start in one command guarantees
 * they agree. Set E2E_START_CMD to just the start half when you have already
 * built with the right env.
 */
const startCommand =
  process.env.E2E_START_CMD ?? `npm run build && npm run start -- --port ${PORT}`;

export default defineConfig({
  testDir: "./e2e",

  // The dashboard mounts ~95 components and polls 47 endpoints; a cold Next
  // compile of that route in dev mode is the slow part, not the assertions.
  // Run against a built server (see the README) and the first navigation drops
  // from tens of seconds to hundreds of milliseconds.
  timeout: 120_000,
  expect: { timeout: 15_000 },

  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  // Each worker is a separate browser context AND a separate poll storm against
  // the same dev server; two is a deliberate ceiling, not a shrug.
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI
    ? [["github"], ["html", { open: "never" }], ["list"]]
    : [["list"]],

  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 15_000,
    navigationTimeout: 90_000,
  },

  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // Load-bearing. app/phase29-enterprise.css hides the sidebar and the
        // topbar pills (including Logout) below 1180px in favour of a burger
        // menu, so a narrower viewport would fail the nav and logout specs for
        // a reason that has nothing to do with the behaviour under test.
        viewport: { width: 1440, height: 900 },
      },
    },
  ],

  // E2E_BASE_URL means "a server is already running there" - attach, don't spawn.
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: startCommand,
        // /login rather than / so the auth route is compiled before the first
        // spec asks for it.
        url: `${BASE_URL}/login`,
        reuseExistingServer: !process.env.CI,
        timeout: 240_000,
        stdout: "pipe",
        stderr: "pipe",
        env: LIVE_API
          ? {}
          : {
              // lib/api.ts inlines this at compile time. Pointing it at a
              // same-origin path keeps the mocked fetches free of CORS
              // preflight - see e2e/support/env.ts for why that matters.
              NEXT_PUBLIC_API_URL: MOCK_API_BASE,
            },
      },
});
