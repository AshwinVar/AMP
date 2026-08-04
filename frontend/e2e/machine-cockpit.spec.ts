import { expect, type Page, test } from "@playwright/test";

import { kpiValue, nav, openDashboard } from "./support/app";
import { LIVE_API } from "./support/env";

/**
 * The machine drill-down, and the keyboard behaviour of the drawer it opens.
 *
 * Every drawer in this app is `role="dialog" aria-modal="true"` rendered over
 * the page. `aria-modal="true"` is a promise to assistive technology that the
 * rest of the page is inert — and until lib/useModalFocus.ts existed the
 * browser cheerfully kept tabbing through it, so a keyboard user opened the
 * cockpit and their next Tab went to controls behind the overlay that they
 * could not see. That fix has unit tests, but jsdom does not move focus on Tab,
 * so they assert what the hook DOES (pull focus in, redirect an escaping Tab),
 * never what a browser does with it. This file is the missing half: a real
 * browser, real Tab keys, and no reference to the hook at all.
 *
 * The CONTROL matters more than usual here. "Focus is still inside the drawer"
 * is trivially true in a harness where Tab does nothing at all, which is
 * exactly the failure mode a headless browser can have.
 */

async function openMachineHealth(page: Page) {
  await nav(page).getByRole("button", { name: "Machine Health" }).click();
  return page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "Machine Health", level: 2 }) });
}

/** Is the keyboard focus somewhere inside the open drawer? */
function focusIsInDialog(page: Page) {
  return page.evaluate(() => {
    const dialog = document.querySelector('[role="dialog"]');
    return Boolean(dialog && document.activeElement && dialog.contains(document.activeElement));
  });
}

test.describe("machine cockpit", () => {
  test.skip(LIVE_API, "drives a fleet the mocks define by name");

  test.beforeEach(async ({ page }) => {
    await openDashboard(page);
  });

  test("the twin cards summarise the fleet from /machine-health", async ({ page }) => {
    const section = await openMachineHealth(page);

    await expect(kpiValue(section, "Machines")).toHaveText("2");
    // Means (78 + 38) / 2 and (64 + 31) / 2 - both computed in the component.
    await expect(kpiValue(section, "Avg health")).toHaveText("58");
    await expect(kpiValue(section, "Avg OEE")).toHaveText("48%");
    // "At risk" counts, "Watch" does not.
    await expect(kpiValue(section, "Need attention")).toHaveText("1");

    await expect(section.getByRole("button", { name: /SMT Line 1/ })).toBeVisible();
    await expect(section.getByRole("button", { name: /AOI Station/ })).toBeVisible();
  });

  test("opening a machine shows that machine's cockpit detail", async ({ page }) => {
    const section = await openMachineHealth(page);
    await section.getByRole("button", { name: /SMT Line 1/ }).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("heading", { name: "SMT Line 1", level: 2 })).toBeVisible();
    await expect(dialog.getByText(/Machine cockpit/)).toBeVisible();

    // The detail comes from a different endpoint to the card behind it
    // (/machine-health/1, not /machine-health), so these prove the drill-down
    // fetched rather than reusing the summary.
    const healthScore = dialog
      .locator("p")
      .filter({ hasText: /^health score$/ })
      .locator("xpath=preceding-sibling::*[1]");
    await expect(healthScore).toHaveText("78");
    await expect(dialog.getByText("Preventive task overdue by 4 days")).toBeVisible();
    await expect(dialog.getByText("Solder bridging").first()).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Approve" }).first()).toBeVisible();
  });

  test("the drawer keeps Tab inside itself", async ({ page }) => {
    const section = await openMachineHealth(page);

    // CONTROL: Tab moves focus in this browser at all. Without this, the
    // assertions below would pass just as well against a page where the key
    // does nothing.
    await page.keyboard.press("Tab");
    expect(await page.evaluate(() => document.activeElement?.tagName ?? "")).not.toBe("BODY");
    expect(await focusIsInDialog(page)).toBe(false);

    await section.getByRole("button", { name: /SMT Line 1/ }).click();
    await expect(page.getByRole("dialog")).toBeVisible();

    // Focus is pulled in on open (polled: the drawer focuses itself in an
    // effect, a tick after it becomes visible).
    await expect.poll(() => focusIsInDialog(page)).toBe(true);

    // Twelve is more than the drawer holds, so this walks off the end at least
    // once and back to the start. Every stop must still be inside.
    for (let i = 0; i < 12; i += 1) {
      await page.keyboard.press("Tab");
      expect(await focusIsInDialog(page)).toBe(true);
    }

    // And off the front edge, which is the direction the wrap is easiest to
    // get wrong.
    await page.keyboard.press("Shift+Tab");
    expect(await focusIsInDialog(page)).toBe(true);
  });

  test("Escape closes the drawer and gives focus back to the card that opened it", async ({ page }) => {
    const section = await openMachineHealth(page);
    const card = section.getByRole("button", { name: /SMT Line 1/ });
    await card.click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect.poll(() => focusIsInDialog(page)).toBe(true);

    await page.keyboard.press("Escape");

    await expect(dialog).toBeHidden();
    // Returning focus is the half that is easy to forget: without it the next
    // Tab restarts from the top of the document, nowhere near where the user
    // was working.
    await expect(card).toBeFocused();
  });
});
