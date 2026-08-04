import { expect, test } from "@playwright/test";

import { activeViewTitle, kpiValue, nav, openDashboard } from "./support/app";
import { LIVE_API } from "./support/env";

/**
 * The shell: nav, and the numbers.
 *
 * The sidebar is not a static list. It is rendered from the backend module
 * manifest (GET /modules, backend/modules.json), grouped by pack, with the
 * compiled-in NAV_ITEMS as a fallback when that call fails — a "plug and play"
 * arrangement whose failure mode is an empty or half-empty sidebar, i.e. a
 * product that looks like it lost half its features.
 *
 * The KPI tiles are asserted on their VALUES, not their presence. A tile that
 * renders "0" for everything looks identical to a working one in a screenshot,
 * and three of these numbers are computed in the page rather than served
 * (utilisation mean, the parsed downtime total, the modal reason) — the exact
 * kind of arithmetic that has been wrong here before (#38, the duration parser
 * reading "1.5 hrs" as 5 hours).
 */

test.describe("dashboard shell", () => {
  test.beforeEach(async ({ page }) => {
    await openDashboard(page);
  });

  test("renders the manifest-driven nav, grouped by pack", async ({ page }) => {
    const sidebar = nav(page);

    // Group headers come from the manifest's packs...
    await expect(sidebar.getByText("Core MES", { exact: true })).toBeVisible();
    await expect(sidebar.getByText("Operations Pack", { exact: true })).toBeVisible();
    await expect(sidebar.getByText("Factory Pack", { exact: true })).toBeVisible();

    // ...and the items from each pack's views. One per pack is enough to prove
    // the flattening; role filtering is role-gating.spec.ts's job.
    await expect(sidebar.getByRole("button", { name: "Mission Control" })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: "Work Orders" })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: "Machine Health" })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: "User Management" })).toBeVisible();
  });

  test("a nav item switches the view and the topbar title follows it", async ({ page }) => {
    await nav(page).getByRole("button", { name: "Overview" }).click();

    await expect(activeViewTitle(page)).toHaveText("Overview");
    await expect(page.getByRole("heading", { name: "AMP Dashboard" })).toBeVisible();
  });

  test("the overview KPI tiles report the API's numbers", async ({ page }) => {
    test.skip(LIVE_API, "asserts on the mocked fixture values");

    await nav(page).getByRole("button", { name: "Overview" }).click();

    const main = page.locator("main");

    // Straight from GET /machines: 3 machines, 2 Running, 1 Breakdown.
    await expect(kpiValue(main, "Running Machines")).toHaveText("2");
    await expect(kpiValue(main, "Breakdowns")).toHaveText("1");
    // Computed in the page: (82 + 74 + 12) / 3.
    await expect(kpiValue(main, "Avg Utilization")).toHaveText("56%");

    // Straight from GET /downtime-logs, then through lib/duration:
    // "45 min" + "30 min" + "15 min".
    await expect(kpiValue(main, "Downtime Events")).toHaveText("3");
    await expect(kpiValue(main, "Total Downtime")).toHaveText("90m");
    // Two of the three logs share a reason.
    await expect(kpiValue(main, "Top Reason")).toHaveText("Material Shortage");
  });

  test("the executive scorecard strip renders its KPIs", async ({ page }) => {
    test.skip(LIVE_API, "asserts on the mocked fixture values");

    await nav(page).getByRole("button", { name: "Overview" }).click();

    // The strip renders nothing at all unless /scorecard says has_data, so a
    // visible value proves the whole fetch-and-format path, including the
    // percent-suffix branch that once printed currency as "49740£".
    await expect(page.getByText("68.4%")).toBeVisible();
    await expect(page.getByText("97.2%")).toBeVisible();
  });
});
