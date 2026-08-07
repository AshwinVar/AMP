import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import LockedModuleView from "./LockedModuleView";
import { MODULE_CATALOG, catalogFromPacks, type ModulePack } from "../lib/modules";

/**
 * The locked screen has to work for a pack the frontend has never heard of.
 *
 * #304's promise is that a pack can be added to backend/modules.json and appear
 * in the product with no frontend release. The nav honours that — the dashboard
 * builds its nav from the live manifest, and getViewModuleIn resolves against
 * that list rather than the compiled-in table.
 *
 * The LOCKED screen did not. It looked the pack up in MODULE_CATALOG, the static
 * array baked into the bundle, and returned `null` on a miss. So a manifest-only
 * pack that the tenant's plan does not cover rendered as an entirely blank pane:
 * no lock icon, no description, no "Contact Sales to Unlock". The one screen
 * whose whole job is to sell the upgrade showed nothing at all — and it failed
 * silently, because `return null` is not an error.
 *
 * That is the shape of bug a component test catches and nothing else does. It is
 * invisible to lib/ tests (the resolution logic is correct), and a browser test
 * would only see it if someone thought to lock that exact pack.
 */

/** A pack shaped like one the backend added after this bundle was built. */
function manifestPack(over: Partial<ModulePack> = {}): ModulePack {
  return {
    id: "traceability",
    label: "Traceability",
    description: "Genealogy and full forward/backward trace for every serial.",
    tagline: "Prove it",
    color: "purple",
    gated: true,
    plans: ["enterprise"],
    enabled: false,
    views: [{ key: "genealogy", label: "Genealogy", icon: "◇" }],
    ...over,
  };
}

describe("LockedModuleView", () => {
  it("sells a pack that only the live manifest knows about", () => {
    // The regression, stated as the product outcome: a tenant clicking a locked
    // pack must be told what it is and how to get it.
    render(
      <LockedModuleView
        moduleKey="traceability"
        catalog={catalogFromPacks([manifestPack()])}
      />,
    );

    expect(screen.getByRole("heading", { name: "Traceability" })).toBeTruthy();
    expect(screen.getByText(/Genealogy and full forward\/backward trace/)).toBeTruthy();
    expect(screen.getByText("Prove it")).toBeTruthy();
    const cta = screen.getByRole("link", { name: /Contact Sales to Unlock/i });
    expect(cta.getAttribute("href")).toBe("mailto:info@marx8.com");
  });

  it("still renders the built-in packs from the static catalogue", () => {
    // CONTROL. Without this, "always render something" could be satisfied by a
    // component that ignored the catalogue entirely and printed a generic
    // screen — which would lose the per-pack copy that makes this screen worth
    // showing. A known pack must still get its own label and description.
    const factory = MODULE_CATALOG.find((m) => m.key === "factory")!;

    render(<LockedModuleView moduleKey="factory" />);

    expect(screen.getByRole("heading", { name: factory.label })).toBeTruthy();
    expect(screen.getByText(factory.description)).toBeTruthy();
  });

  it("never renders an empty pane, even for a key nothing can resolve", () => {
    // Defence in depth. Passing the live catalogue fixes the reachable case, but
    // `return null` was the actual failure mode and it is worth removing outright
    // — a blank screen tells the user their click did nothing, and tells whoever
    // is debugging it even less. A generic upgrade prompt is always better than
    // nothing, so an unresolvable key must still produce the CTA.
    render(<LockedModuleView moduleKey="not-a-pack-anyone-knows" catalog={[]} />);

    expect(screen.getByRole("link", { name: /Contact Sales to Unlock/i })).toBeTruthy();
    expect(screen.getByText(/not included in your current plan/i)).toBeTruthy();
  });

  it("prefers the live catalogue over the static one when both define a pack", () => {
    // A pack's copy can be edited backend-side — that is the point of the
    // manifest. If the bundle's stale description won, the operator would read
    // last release's pitch, and nobody would understand why editing
    // modules.json changed nothing.
    render(
      <LockedModuleView
        moduleKey="factory"
        catalog={catalogFromPacks([
          manifestPack({ id: "factory", label: "Factory Ops (renamed)", description: "New copy from the manifest." }),
        ])}
      />,
    );

    expect(screen.getByRole("heading", { name: "Factory Ops (renamed)" })).toBeTruthy();
    expect(screen.getByText("New copy from the manifest.")).toBeTruthy();
  });
});
