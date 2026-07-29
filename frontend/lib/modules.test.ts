import { describe, expect, it } from "vitest";

import {
  ALWAYS_OPEN_MODULES,
  MODULE_CATALOG,
  NAV_ITEMS,
  PLAN_MODULES,
  canRoleSeeView,
  catalogFromPacks,
  enabledModulesFromPacks,
  getEnabledModules,
  isViewEnabledIn,
  navItemsFromPacks,
  withAlwaysOpen,
  type ModulePack,
} from "./modules";

/**
 * This file decides which screens a tenant can see. Getting it wrong either
 * hides something they pay for or shows something they do not - and it had no
 * tests at all.
 *
 * The backend manifest (backend/modules.json) carries two independent fields
 * that are easy to conflate:
 *
 *   gated  - whether the API enforces a 403 on that pack's routes.
 *            `module_manifest.always_open_packs()` is every pack with
 *            gated=false, i.e. {core, admin}.
 *   plans  - which subscription bundles provision the pack when a plan is
 *            applied. `admin` lists only enterprise/demo.
 *
 * So `admin` is never blocked by the API, but is not part of the starter
 * bundle. The live path in the dashboard got this right (it unions the tenant's
 * licence with core+admin); the offline fallback did not, and that is the bug
 * these tests were written against.
 */

const pack = (over: Partial<ModulePack> = {}): ModulePack => ({
  id: "operations",
  label: "Operations Pack",
  description: "",
  tagline: "",
  color: "green",
  gated: true,
  plans: ["growth", "enterprise", "demo"],
  enabled: false,
  views: [{ key: "workorders", label: "Work Orders", icon: "▣" }],
  ...over,
});

describe("the always-open overlay", () => {
  it("mirrors the packs the API never gates", () => {
    // backend/modules.json: core and admin are the only gated=false packs, and
    // module_manifest.always_open_packs() returns exactly those two. If that
    // manifest changes, this is the line that should force a look.
    expect([...ALWAYS_OPEN_MODULES].sort()).toEqual(["admin", "core"]);
  });

  it("grants the always-open packs to a plan that does not bundle them", () => {
    // THE BUG. The starter bundle is [core] — admin is enterprise/demo only —
    // but the API never gates admin routes, and the live path always grants it.
    // The fallback used to return the bare bundle, so a starter tenant lost
    // User Management, Documents, Costing and Enterprise Polish from the nav
    // whenever /tenant-config was slow or failed.
    expect(getEnabledModules("starter")).toContain("admin");
    expect(getEnabledModules("starter")).toContain("core");
  });

  it("does not invent anything the plan has not bought", () => {
    const starter = getEnabledModules("starter");
    expect(starter).not.toContain("operations");
    expect(starter).not.toContain("factory");
    expect(starter).not.toContain("intelligence");
  });

  it("leaves a full plan unchanged", () => {
    expect(getEnabledModules("enterprise").sort()).toEqual(
      ["admin", "core", "factory", "intelligence", "operations"],
    );
  });

  it("falls back to the always-open set for an unknown plan", () => {
    // An unrecognised plan name must not lock someone out of the basics.
    expect(getEnabledModules("nonsense" as never).sort()).toEqual(["admin", "core"]);
  });

  it("never duplicates a module the plan already bundles", () => {
    const enterprise = getEnabledModules("enterprise");
    expect(new Set(enterprise).size).toBe(enterprise.length);
  });

  it("is the same overlay the manifest path applies", () => {
    // The live path and the fallback must agree, or the nav changes shape when
    // one request fails. Both go through withAlwaysOpen now.
    const packs = [pack({ id: "core", gated: false, enabled: true, plans: [] })];
    expect(enabledModulesFromPacks(packs).sort()).toEqual(withAlwaysOpen([]).sort());
  });
});

describe("enabledModulesFromPacks", () => {
  it("enables a gated pack the tenant has bought", () => {
    const result = enabledModulesFromPacks([pack({ enabled: true })]);
    expect(result).toContain("operations");
  });

  it("withholds a gated pack the tenant has not bought", () => {
    const result = enabledModulesFromPacks([pack({ enabled: false })]);
    expect(result).not.toContain("operations");
  });

  it("enables an ungated pack regardless of the subscription flag", () => {
    // gated=false means the API will serve it whatever the licence says, so
    // hiding it in the UI would only confuse.
    //
    // Deliberately NOT admin: admin is in the always-open overlay, so it comes
    // back either way and this assertion would pass even with the `!p.gated`
    // branch deleted. Mutation testing caught exactly that. `factory` is a pack
    // the overlay does not cover, so the branch is the only thing that can
    // enable it here — which is the point being pinned.
    const result = enabledModulesFromPacks([
      pack({ id: "factory", gated: false, enabled: false }),
    ]);
    expect(result).toContain("factory");
  });
});

describe("role gating", () => {
  it("keeps cross-tenant SaaS admin to the founder", () => {
    expect(canRoleSeeView("saas", "Admin", false)).toBe(false);
    expect(canRoleSeeView("saas", "Admin", true)).toBe(true);
  });

  it("holds an Operator to the shop floor", () => {
    expect(canRoleSeeView("machines", "Operator", false)).toBe(true);
    expect(canRoleSeeView("operator", "Operator", false)).toBe(true);
    expect(canRoleSeeView("costing", "Operator", false)).toBe(false);
    expect(canRoleSeeView("users", "Operator", false)).toBe(false);
  });

  it("lets a Supervisor run the floor but not the account", () => {
    expect(canRoleSeeView("quality", "Supervisor", false)).toBe(true);
    expect(canRoleSeeView("users", "Supervisor", false)).toBe(false);
    expect(canRoleSeeView("enterprise", "Supervisor", false)).toBe(false);
  });

  it("does not let founder status override the Operator restriction", () => {
    // isFounder is about cross-tenant reach, not seniority.
    expect(canRoleSeeView("costing", "Operator", true)).toBe(false);
  });
});

describe("manifest-driven nav", () => {
  it("flattens packs into nav items, keeping the pack as the module", () => {
    const items = navItemsFromPacks([pack()]);
    expect(items).toEqual([
      { key: "workorders", label: "Work Orders", icon: "▣", module: "operations" },
    ]);
  });

  it("survives a pack with no views", () => {
    expect(navItemsFromPacks([pack({ views: undefined as never })])).toEqual([]);
  });

  it("keeps the catalogue in pack order", () => {
    const cat = catalogFromPacks([pack({ id: "factory" }), pack({ id: "core" })]);
    expect(cat.map((c) => c.key)).toEqual(["factory", "core"]);
  });
});

describe("view enablement", () => {
  const nav = NAV_ITEMS;

  it("allows a view whose pack the tenant has", () => {
    expect(isViewEnabledIn("workorders", ["core", "operations"], nav)).toBe(true);
  });

  it("blocks a view whose pack the tenant lacks", () => {
    expect(isViewEnabledIn("workorders", ["core"], nav)).toBe(false);
  });

  it("blocks an unknown view rather than defaulting it open", () => {
    expect(isViewEnabledIn("no-such-view", ["core", "admin"], nav)).toBe(false);
  });
});

describe("the static tables agree with each other", () => {
  it("every nav item belongs to a module the catalogue describes", () => {
    const known = new Set(MODULE_CATALOG.map((m) => m.key));
    const orphans = NAV_ITEMS.filter((n) => !known.has(n.module)).map((n) => n.key);
    expect(orphans).toEqual([]);
  });

  it("every plan bundle names a real module", () => {
    const known = new Set(MODULE_CATALOG.map((m) => m.key));
    for (const [plan, mods] of Object.entries(PLAN_MODULES)) {
      const unknown = mods.filter((m) => !known.has(m));
      expect(unknown, `plan ${plan}`).toEqual([]);
    }
  });

  it("has no duplicate view keys, which would make gating ambiguous", () => {
    const keys = NAV_ITEMS.map((n) => n.key);
    expect(new Set(keys).size).toBe(keys.length);
  });
});
