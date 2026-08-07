"use client";

import { MODULE_CATALOG, type ModuleInfo, type ModuleKey } from "../lib/modules";

const COLOR_STYLES: Record<string, { border: string; badge: string; text: string }> = {
  blue:   { border: "border-blue-500/40",   badge: "bg-blue-500/10 text-blue-400",   text: "text-blue-400" },
  green:  { border: "border-green-500/40",  badge: "bg-green-500/10 text-green-400", text: "text-green-400" },
  purple: { border: "border-purple-500/40", badge: "bg-purple-500/10 text-purple-400", text: "text-purple-400" },
  amber:  { border: "border-amber-500/40",  badge: "bg-amber-500/10 text-amber-400", text: "text-amber-400" },
  red:    { border: "border-red-500/40",    badge: "bg-red-500/10 text-red-400",     text: "text-red-400" },
};

/**
 * The upgrade screen for a pack the tenant's plan does not cover.
 *
 * `catalog` is the LIVE one, built from GET /modules — pass it. It defaults to
 * the compiled-in MODULE_CATALOG only so a caller that has not loaded the
 * manifest yet still renders something sensible.
 *
 * Why the prop exists at all: this component used to look packs up in
 * MODULE_CATALOG unconditionally and `return null` on a miss, while the
 * dashboard resolved the key from the LIVE nav. A pack added to
 * backend/modules.json after this bundle shipped therefore rendered as a blank
 * pane — no icon, no copy, no call to action — which is the exact opposite of
 * what a locked screen is for, and it failed silently because rendering nothing
 * is not an error.
 */
export default function LockedModuleView({
  moduleKey,
  catalog = MODULE_CATALOG,
}: {
  moduleKey: ModuleKey;
  catalog?: ModuleInfo[];
}) {
  // Live first, static second. A pack present in both is described by the
  // manifest, because editing modules.json is how its copy is meant to change;
  // preferring the bundle would show last release's pitch and make the edit look
  // like it did nothing.
  const mod =
    catalog.find((m) => m.key === moduleKey) ??
    MODULE_CATALOG.find((m) => m.key === moduleKey) ??
    // Nothing resolved it. Still show the upgrade path rather than an empty
    // screen: a generic prompt is worth more than a blank pane to the user, and
    // far more to whoever has to debug it.
    ({
      key: moduleKey,
      label: "This module",
      description: "",
      tagline: "Locked",
      color: "blue",
    } satisfies ModuleInfo);

  const colors = COLOR_STYLES[mod.color] ?? COLOR_STYLES.blue;

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] text-center px-6">
      <div className="text-5xl mb-6 opacity-40">🔒</div>

      <span className={`text-xs font-bold tracking-widest uppercase px-3 py-1 rounded-full border mb-4 ${colors.badge} ${colors.border}`}>
        {mod.tagline}
      </span>

      <h2 className="text-3xl font-bold text-white mb-3">{mod.label}</h2>
      {/* Conditional so the unresolved-pack fallback, which has no description,
          does not leave an empty paragraph holding its bottom margin open. */}
      {mod.description && (
        <p className="text-slate-400 max-w-md mb-8 text-base leading-relaxed">{mod.description}</p>
      )}

      <div className={`rounded-2xl border ${colors.border} bg-slate-900 p-6 max-w-sm w-full`}>
        <p className="text-slate-300 text-sm mb-5">
          This module is not included in your current plan. Contact us to unlock it for your workspace.
        </p>
        <a
          href="mailto:info@marx8.com"
          className="block w-full rounded-xl bg-white text-slate-950 font-semibold py-3 text-sm hover:opacity-90 transition"
        >
          Contact Sales to Unlock
        </a>
      </div>
    </div>
  );
}
