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
  getViewModule,
  getViewModuleIn,
  isViewEnabled,
  isViewEnabledIn,
  navItemsFromPacks,
  withAlwaysOpen,
  type ModuleKey,
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

  it("resolves an unknown view to a pack the locked screen can actually render", () => {
    // renderSection pairs these two: when isViewEnabledIn says no, the module
    // key goes straight into <LockedModuleView>, which does
    // MODULE_CATALOG.find(...) and returns null on a miss. An unknown view key
    // is exactly the case that reaches here — isViewEnabledIn already returned
    // false for it — so without the `?? "core"` fallback the user gets a blank
    // pane where the "Contact Sales to Unlock" upsell should be.
    expect(getViewModuleIn("no-such-view", nav)).toBe("core");
    expect(getViewModule("no-such-view")).toBe("core");
    expect(MODULE_CATALOG.map((m) => m.key)).toContain(getViewModuleIn("no-such-view", nav));

    // Control: a view the nav does know must report its own pack, not the
    // fallback — otherwise every locked screen would advertise Core MES.
    expect(getViewModuleIn("workorders", nav)).toBe("operations");
    expect(getViewModule("workorders")).toBe("operations");
  });

  it("gates a view the live manifest added, without a frontend release", () => {
    // The whole point of the manifest path (#304): a pack added to
    // backend/modules.json must gate and label correctly against the live nav
    // list even though NAV_ITEMS here has never heard of it. If getViewModuleIn
    // consulted the compiled-in table instead of the list it was handed, the new
    // pack's locked screen would pitch Core MES.
    // THE CAST IS PART OF THE POINT, not a convenience. `ModuleKey` is a closed
    // union of the five packs that existed when it was written, so a pack the
    // backend adds later is literally unrepresentable in the frontend's types —
    // which is the type-level shape of the very gap #304 set out to remove.
    // The runtime path handles it correctly, as the assertions below show; only
    // the type does not. Widen ModuleKey to `string` (or derive it from the
    // manifest) and this cast becomes unnecessary.
    const NEW_PACK = "traceability" as ModuleKey;

    const liveNav = navItemsFromPacks([
      pack({ id: NEW_PACK, views: [{ key: "genealogy", label: "Genealogy", icon: "◇" }] }),
    ]);

    expect(getViewModuleIn("genealogy", liveNav)).toBe(NEW_PACK);
    expect(isViewEnabledIn("genealogy", ["core", NEW_PACK], liveNav)).toBe(true);
    expect(isViewEnabledIn("genealogy", ["core", "admin"], liveNav)).toBe(false);
  });
});

describe("the offline fallback gate", () => {
  // isViewEnabled/getViewModule are the compiled-in twins of the *In variants:
  // they answer from NAV_ITEMS, which is what the dashboard falls back to when
  // GET /modules can't be reached. #413 was a fallback-only bug, so this is the
  // path that goes wrong while everything looks fine on a healthy network.

  it("opens the never-gated screens for a starter tenant while /modules is down", () => {
    // #413 end to end: the starter bundle is [core], but the API never gates the
    // admin pack, so User Management / Documents / Costing keep serving 200s.
    // A fallback that hid them left the user staring at a "Contact Sales" screen
    // for pages their own tenant was already allowed to open.
    const starter = getEnabledModules("starter");

    expect(isViewEnabled("users", starter)).toBe(true);
    expect(isViewEnabled("documents", starter)).toBe(true);
    expect(isViewEnabled("machines", starter)).toBe(true);

    // Control: the overlay must not open everything. Operations is genuinely
    // unlicensed on starter, and the API really does 403 it.
    expect(isViewEnabled("workorders", starter)).toBe(false);
  });

  it("fails closed on a view key it does not recognise", () => {
    // A renamed or typo'd view key must lock, not inherit the last thing the
    // caller happened to have enabled. Note the deliberate asymmetry with
    // getViewModule, which fails *open* to "core": one decides access, the
    // other only decides which upsell copy to print.
    const everything: ModuleKey[] = ["core", "operations", "factory", "intelligence", "admin"];

    expect(isViewEnabled("no-such-view", everything)).toBe(false);
    // Control: with the same module list a real view opens, so the false above
    // is the unknown key being rejected and not a gate that never opens.
    expect(isViewEnabled("quality", everything)).toBe(true);
  });

  it("answers identically to the manifest pair for the same nav list", () => {
    // The dashboard swaps navItems between the manifest and NAV_ITEMS depending
    // on whether one fetch succeeded. If the two pairs disagreed, a screen would
    // open or lock based purely on that request's luck — the nav changing shape
    // under the user is the failure mode #413 was about.
    const mods: ModuleKey[] = ["core", "admin", "operations"];

    for (const key of [...NAV_ITEMS.map((n) => n.key), "no-such-view"]) {
      expect(isViewEnabled(key, mods), key).toBe(isViewEnabledIn(key, mods, NAV_ITEMS));
      expect(getViewModule(key), key).toBe(getViewModuleIn(key, NAV_ITEMS));
    }
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
