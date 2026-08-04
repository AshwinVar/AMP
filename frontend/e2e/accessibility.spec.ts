import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

import { nav, openDashboard } from "./support/app";
import { installMockApi } from "./support/mock-api";

/**
 * An automated floor, not a ceiling.
 *
 * Be clear about what this proves: axe finds the machine-checkable subset of
 * WCAG — missing labels and alt text, broken ARIA, insufficient contrast,
 * duplicate ids, unlabelled controls. Published estimates put that at roughly a
 * third of real accessibility defects, and the two-thirds it cannot see are the
 * ones that matter most on a shop-floor dashboard: whether the focus order
 * makes sense, whether an alert is announced when it appears, whether a live
 * OEE tile updating every three seconds is comprehensible to a screen reader
 * user, whether the language on screen means anything. A green run here means
 * "no known machine-detectable violations", never "accessible".
 *
 * The keyboard behaviour that axe cannot check is tested separately, by driving
 * real Tab and Escape keys — see machine-cockpit.spec.ts.
 *
 * Only serious and critical impacts fail. Minor and moderate findings are worth
 * fixing but are not worth blocking a deploy over, and treating everything as
 * equal is how a11y gates end up switched off entirely.
 */

/**
 * Violations that already existed when this suite was written, itemised down to
 * the offending element rather than silenced by rule id — a blanket
 * "ignore color-contrast" would hide every future contrast bug on every screen.
 *
 * The list can only shrink: a baselined entry that no longer fires ALSO fails
 * the test, with instructions to delete it. That is what stops a baseline from
 * quietly becoming permanent.
 */
type KnownViolation = { rule: string; target: string; note: string };

const KNOWN_LOGIN_VIOLATIONS: KnownViolation[] = [
  {
    rule: "color-contrast",
    target: ".mt-6",
    // "Accounts are created by your administrator." - text-slate-500 (#64748b)
    // on bg-slate-900 (#0f172a) is 3.4:1 where 4.5:1 is required. Fix is one
    // class in app/login/page.tsx: text-slate-500 -> text-slate-400 (6.3:1).
    note: "login footnote is text-slate-500 on slate-900",
  },
];

const KNOWN_DASHBOARD_VIOLATIONS: KnownViolation[] = [
  {
    rule: "label-title-only",
    target: "select",
    // The founder company switcher in the topbar carries only title="Switch
    // company / tenant". A title is not a label: it never appears for keyboard
    // users and screen readers treat it as a last resort. Fix is an aria-label
    // (or a visible label) on the <select> in app/dashboard/page.tsx.
    note: "tenant switcher is labelled by title only",
  },
];

type Audit = { unexpected: string[]; fixed: string[] };

async function audit(page: Page, known: KnownViolation[]): Promise<Audit> {
  const results = await new AxeBuilder({ page })
    // <nextjs-portal> is the dev-mode error overlay injected by the framework.
    // Absent from a production build, so this only matters if someone points
    // E2E_START_CMD at `next dev`; failing on it would be failing on Next.
    .exclude("nextjs-portal")
    .analyze();

  const blocking = results.violations.filter(
    (violation) => violation.impact === "serious" || violation.impact === "critical",
  );

  // One entry per offending NODE, not per rule: a second element breaking an
  // already-baselined rule has to fail.
  const found = blocking.flatMap((violation) =>
    violation.nodes.map((node) => ({
      key: `${violation.id} @ ${String(node.target)}`,
      detail: `${violation.impact} | ${violation.id} | ${violation.help} | ${String(node.target)}`,
    })),
  );

  const knownKeys = new Set(known.map((k) => `${k.rule} @ ${k.target}`));
  const foundKeys = new Set(found.map((f) => f.key));

  return {
    unexpected: found.filter((f) => !knownKeys.has(f.key)).map((f) => f.detail),
    fixed: known.filter((k) => !foundKeys.has(`${k.rule} @ ${k.target}`)).map((k) => `${k.rule} @ ${k.target} (${k.note})`),
  };
}

const STALE = "already fixed in the app - delete it from the baseline in this file";

test.describe("accessibility", () => {
  test("the login page has no new serious or critical violations", async ({ page }) => {
    await installMockApi(page);
    await page.goto("/login");
    await expect(page.getByRole("button", { name: "Login" })).toBeVisible();

    const result = await audit(page, KNOWN_LOGIN_VIOLATIONS);
    expect(result.unexpected).toEqual([]);
    expect(result.fixed, STALE).toEqual([]);
  });

  test("the dashboard has no new serious or critical violations", async ({ page }) => {
    await openDashboard(page);

    // Scanned on Mission Control, the default view: the screen a user actually
    // lands on, with its nav, pulse header and live feed all mounted.
    await expect(nav(page)).toBeVisible();
    await expect(page.getByRole("heading", { name: "Mission Control", level: 2 })).toBeVisible();

    const result = await audit(page, KNOWN_DASHBOARD_VIOLATIONS);
    expect(result.unexpected).toEqual([]);
    expect(result.fixed, STALE).toEqual([]);
  });
});
