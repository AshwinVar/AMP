import { expect, test } from "@playwright/test";

import { nav, openDashboard } from "./support/app";
import { LIVE_API } from "./support/env";

/**
 * What an Operator is allowed to see.
 *
 * `canRoleSeeView` (lib/modules.ts) is unit-tested as a function. What is not
 * tested is that the sidebar actually applies it — and the sidebar is where the
 * decision is visible to a customer. The rule has three layers that interact:
 * an Operator is limited to OPERATOR_VIEWS, a Supervisor is refused the
 * owner/account screens, and a whole nav GROUP disappears when the role can see
 * none of its views. The Admin Pack is exactly that case for an Operator, which
 * means the failure mode is not a stray link, it is a shop-floor terminal
 * offering User Management and cross-tenant SaaS Admin.
 *
 * Both directions are asserted on the same page with the same fixtures,
 * because "the Operator cannot see User Management" is also true of a nav that
 * failed to render at all.
 */

test.describe("role gating in the nav", () => {
  test.skip(LIVE_API, "the role comes from whoever E2E_USER is");

  const ADMIN_ONLY = ["User Management", "SaaS Admin", "Enterprise Polish"];

  test("an Operator gets the shop-floor views and none of the admin ones", async ({ page }) => {
    await openDashboard(page, { session: { username: "olga", role: "Operator" } });

    const sidebar = nav(page);

    // The screens an Operator works in (OPERATOR_VIEWS).
    await expect(sidebar.getByRole("button", { name: "Mission Control" })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: "Machines" })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: "Work Orders" })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: "Operator Terminal" })).toBeVisible();

    // And the ones they are not: management reporting, and buying things.
    await expect(sidebar.getByRole("button", { name: "Analytics" })).toHaveCount(0);
    await expect(sidebar.getByRole("button", { name: "Purchasing" })).toHaveCount(0);

    for (const label of ADMIN_ONLY) {
      await expect(sidebar.getByRole("button", { name: label })).toHaveCount(0);
    }

    // Not one admin view survives the filter, so the group header goes too -
    // an empty "Admin Pack" heading would be its own kind of tease.
    await expect(sidebar.getByText("Admin Pack", { exact: true })).toHaveCount(0);
  });

  test("CONTROL: an Admin on the same page sees every one of them", async ({ page }) => {
    await openDashboard(page, { session: { username: "alice", role: "Admin" } });

    const sidebar = nav(page);

    for (const label of ADMIN_ONLY) {
      await expect(sidebar.getByRole("button", { name: label })).toBeVisible();
    }
    await expect(sidebar.getByText("Admin Pack", { exact: true })).toBeVisible();
  });

  test("a Supervisor keeps the floor but loses account administration", async ({ page }) => {
    await openDashboard(page, { session: { username: "sam", role: "Supervisor" } });

    const sidebar = nav(page);

    // Documents and Costing are Admin Pack views a Supervisor DOES get: the
    // pack is not the unit of the decision, the view is.
    await expect(sidebar.getByRole("button", { name: "Documents" })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: "Costing" })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: "Purchasing" })).toBeVisible();

    for (const label of ADMIN_ONLY) {
      await expect(sidebar.getByRole("button", { name: label })).toHaveCount(0);
    }
  });
});
