import { expect, test } from "@playwright/test";

import { activeViewTitle, kpiValue, nav, openDashboard } from "./support/app";
import { LIVE_API } from "./support/env";

/**
 * Mission Control is the view the product opens on.
 *
 * `useState("mission")` in app/dashboard/page.tsx is the whole of that
 * decision, and two later effects can move it: a non-founder tenant is sent to
 * "inventory" on mount, and a licence that does not cover the current view
 * snaps to "overview". So "the first screen a user sees" is an emergent result
 * of three pieces of code, not a constant — and it is the screen every demo
 * starts on.
 *
 * The default-view assertion needs the CONTROL below to mean anything: a
 * topbar that said "Mission Control" no matter which view was active would
 * pass it just as happily.
 */

test.describe("Mission Control", () => {
  test.beforeEach(async ({ page }) => {
    await openDashboard(page);
  });

  test("is the view the dashboard opens on", async ({ page }) => {
    await expect(activeViewTitle(page)).toHaveText("Mission Control");
    await expect(page.getByRole("heading", { name: "Mission Control", level: 2 })).toBeVisible();
  });

  test("CONTROL: the topbar title tracks the active view", async ({ page }) => {
    await nav(page).getByRole("button", { name: "Overview" }).click();

    await expect(activeViewTitle(page)).toHaveText("Overview");
    // The section unmounts, so the title above was reporting the view rather
    // than being a fixed banner.
    await expect(page.getByRole("heading", { name: "Mission Control", level: 2 })).toHaveCount(0);
  });

  test("renders the pulse header and the insight counts", async ({ page }) => {
    test.skip(LIVE_API, "asserts on the mocked fixture values");

    const section = page
      .locator("section")
      .filter({ has: page.getByRole("heading", { name: "Mission Control", level: 2 }) });

    // FactoryPulse: its own fetch, above the counts.
    await expect(section.getByText("One machine needs attention before the next shift.")).toBeVisible();
    await expect(kpiValue(section, "Fleet health")).toHaveText("71");

    // Counts derived in the component from the three insights served.
    await expect(kpiValue(section, "Active insights")).toHaveText("3");
    await expect(kpiValue(section, "Critical")).toHaveText("1");
    await expect(kpiValue(section, "High")).toHaveText("1");
    await expect(kpiValue(section, "AI recommendations")).toHaveText("2");

    // And the feed itself, not just the tallies.
    await expect(section.getByText("AOI Station is trending to failure")).toBeVisible();
  });
});
