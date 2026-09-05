/**
 * A pack id.
 *
 * The five literals are the packs that ship in this bundle — listing them keeps
 * editor autocomplete and still catches a typo in PLAN_MODULES or
 * ALWAYS_OPEN_MODULES, where a wrong key silently un-gates or over-gates a
 * module.
 *
 * `(string & {})` widens it to accept any other id WITHOUT collapsing the union
 * to plain `string` (which would erase both the autocomplete and the typo
 * check). That is not decoration: #304's promise is that a pack can be added to
 * backend/modules.json and appear in the product with no frontend release, and
 * a closed union made such a pack literally inexpressible in this file's own
 * types — every function handling one needed an `as ModuleKey` cast, and the
 * casts are what let LockedModuleView's blank-pane bug through typechecking.
 */
export type ModuleKey =
  | "core"
  | "operations"
  | "factory"
  | "intelligence"
  | "admin"
  | (string & {});
export type PlanName = "starter" | "growth" | "enterprise" | "demo";

export type NavItem = {
  key: string;
  label: string;
  icon: string;
  module: ModuleKey;
};

export const NAV_ITEMS: NavItem[] = [
  { key: "mission",        label: "Mission Control",    icon: "❖", module: "core" },
  { key: "overview",       label: "Overview",          icon: "⌂", module: "core" },
  { key: "machines",       label: "Machines",           icon: "▦", module: "core" },
  { key: "downtime",       label: "Downtime",           icon: "◷", module: "core" },
  { key: "shifts",         label: "Shifts",             icon: "◴", module: "core" },
  { key: "analytics",      label: "Analytics",          icon: "▧", module: "core" },
  { key: "trends",         label: "Trends",             icon: "▨", module: "core" },
  { key: "timeline",       label: "Timeline",           icon: "↔", module: "core" },
  // CORE, deliberately — not the Factory pack. This is where a customer sees
  // and withdraws what a machine's manufacturer can read about their shop
  // floor, and a consent control behind a paywall is not a consent control.
  { key: "connected",      label: "Connected Equipment", icon: "◈", module: "core" },
  { key: "workorders",     label: "Work Orders",        icon: "▣", module: "operations" },
  { key: "planning",       label: "Production Plan",    icon: "▤", module: "operations" },
  { key: "scheduling",     label: "Scheduling",         icon: "◫", module: "operations" },
  { key: "operator",       label: "Operator Terminal",  icon: "▶", module: "operations" },
  { key: "orders",         label: "Orders & Dispatch",  icon: "⇄", module: "operations" },
  { key: "maintenance_ai", label: "Maintenance AI",     icon: "◇", module: "factory" },
  { key: "cmms",           label: "CMMS",               icon: "⚙", module: "factory" },
  { key: "quality",        label: "Quality",            icon: "✓", module: "factory" },
  { key: "inventory",      label: "Inventory",          icon: "▥", module: "factory" },
  { key: "purchasing",     label: "Purchasing",         icon: "◈", module: "factory" },
  { key: "digitaltwin",    label: "Digital Twin",       icon: "◎", module: "factory" },
  { key: "machinehealth",  label: "Machine Health",     icon: "♥", module: "factory" },
  { key: "iot",            label: "IoT Command",        icon: "◉", module: "intelligence" },
  { key: "connectivity",   label: "Connectivity",       icon: "⇄", module: "intelligence" },
  { key: "ai",             label: "AI Insights",        icon: "✦", module: "intelligence" },
  { key: "copilot",        label: "AI Copilot",         icon: "✸", module: "intelligence" },
  { key: "inbox",          label: "Inbox",              icon: "✉", module: "intelligence" },
  { key: "agentactivity",  label: "Agent Activity",     icon: "⊙", module: "intelligence" },
  { key: "roi",            label: "AI Impact",          icon: "◊", module: "intelligence" },
  { key: "executive",      label: "Executive OEE",      icon: "▰", module: "intelligence" },
  { key: "escalations",    label: "Escalations",        icon: "!", module: "intelligence" },
  { key: "notifications",  label: "Notifications",      icon: "●", module: "intelligence" },
  { key: "documents",      label: "Documents",          icon: "▱", module: "admin" },
  { key: "saas",           label: "SaaS Admin",         icon: "◌", module: "admin" },
  { key: "users",          label: "User Management",    icon: "◔", module: "admin" },
  { key: "costing",        label: "Costing",            icon: "£", module: "admin" },
  { key: "enterprise",     label: "Enterprise Polish",  icon: "◆", module: "admin" },
];

export type ModuleInfo = {
  key: ModuleKey;
  label: string;
  description: string;
  tagline: string;
  color: string;
};

export const MODULE_CATALOG: ModuleInfo[] = [
  {
    key: "core",
    label: "Core MES",
    description: "Real-time machine monitoring, downtime tracking, OEE, and shift performance.",
    tagline: "Included in every plan",
    color: "blue",
  },
  {
    key: "operations",
    label: "Operations Pack",
    description: "Work orders, production planning, scheduling, operator terminal, and order dispatch.",
    tagline: "For production teams",
    color: "green",
  },
  {
    key: "factory",
    label: "Factory Pack",
    description: "Predictive maintenance AI, CMMS, quality inspections, inventory, purchasing, and digital twin.",
    tagline: "Full shopfloor visibility",
    color: "purple",
  },
  {
    key: "intelligence",
    label: "Intelligence Pack",
    description: "IoT command center, AI-driven insights, executive OEE dashboards, and smart escalations.",
    tagline: "Data-driven decisions",
    color: "amber",
  },
  {
    key: "admin",
    label: "Admin Pack",
    description: "Compliance documents, cost tracking, SaaS tenant management, and enterprise reporting.",
    tagline: "For management & compliance",
    color: "red",
  },
];

export const PLAN_MODULES: Record<PlanName, ModuleKey[]> = {
  starter:    ["core"],
  growth:     ["core", "operations", "factory"],
  enterprise: ["core", "operations", "factory", "intelligence", "admin"],
  demo:       ["core", "operations", "factory", "intelligence", "admin"],
};

// The packs the API never gates, mirroring module_manifest.always_open_packs()
// — every pack in backend/modules.json with `gated: false`. Two fields there are
// easy to conflate: `gated` decides whether the API enforces a 403, while
// `plans` decides what applying a subscription provisions. `admin` is
// gated:false but bundled only with enterprise/demo, so it is always reachable
// even on a plan that does not bundle it.
export const ALWAYS_OPEN_MODULES: readonly ModuleKey[] = ["core", "admin"];

/** A licence plus the packs the API serves regardless of licence. */
export function withAlwaysOpen(modules: readonly string[]): ModuleKey[] {
  return Array.from(new Set<ModuleKey>([...modules, ...ALWAYS_OPEN_MODULES]));
}

export function getEnabledModules(plan: PlanName): ModuleKey[] {
  // The overlay is applied here, not just on the live path. Without it a
  // starter tenant lost User Management, Documents, Costing and Enterprise
  // Polish from the nav whenever /tenant-config was slow or failed — screens
  // the API would have served perfectly well.
  return withAlwaysOpen(PLAN_MODULES[plan] ?? []);
}

export function isViewEnabled(viewKey: string, enabledModules: ModuleKey[]): boolean {
  const item = NAV_ITEMS.find((n) => n.key === viewKey);
  if (!item) return false;
  return enabledModules.includes(item.module);
}

export function getViewModule(viewKey: string): ModuleKey {
  return NAV_ITEMS.find((n) => n.key === viewKey)?.module ?? "core";
}

/**
 * The human name of a view, or null if nothing in the catalogue owns that key.
 *
 * The nav is the ONE place a view's name is written. AICopilot kept its own
 * copy for the "Open <view> →" drill-in, and the copy had drifted: it listed
 * ten of the thirteen views the backend assistant can return, so a shift, WIP
 * or compliance answer silently rendered no button at all — the assistant named
 * a real screen and the UI refused to offer it because its private table had
 * never heard of it. Deriving the label means adding a nav entry is enough.
 *
 * null (not the raw key) for an unknown view, so a caller shows nothing rather
 * than a button reading "Open workorders →".
 */
export function viewLabel(viewKey: string): string | null {
  return NAV_ITEMS.find((n) => n.key === viewKey)?.label ?? null;
}

// ── Role-based view access ────────────────────────────────────────
// Plans gate by feature pack (above); roles gate by who's logged in.
// Admin sees everything (except cross-tenant founder-only views); a
// Supervisor manages the floor but not account/owner administration; an
// Operator only gets the shop-floor execution screens they work in.

// The shop-floor screens an Operator is allowed to open.
export const OPERATOR_VIEWS = new Set<string>([
  "mission", "overview", "machines", "machinehealth", "downtime", "workorders",
  "operator", "quality", "cmms", "inventory", "notifications",
]);

// Owner/account administration — hidden from Supervisors.
export const ADMIN_ONLY_VIEWS = new Set<string>(["users", "saas", "enterprise"]);

// Cross-tenant SaaS administration — only the internal founder (DEFAULT
// tenant) may manage other companies, even a client's own Admin cannot.
export const FOUNDER_ONLY_VIEWS = new Set<string>(["saas"]);

export function canRoleSeeView(viewKey: string, role: string, isFounder: boolean): boolean {
  if (FOUNDER_ONLY_VIEWS.has(viewKey) && !isFounder) return false;
  if (role === "Operator") return OPERATOR_VIEWS.has(viewKey);
  if (role === "Supervisor") return !ADMIN_ONLY_VIEWS.has(viewKey);
  return true; // Admin (and the founder super-admin)
}

// ── Manifest-driven nav (the plug-and-play plugin system) ─────────
// The backend serves the SAME module definitions from modules.json at
// GET /modules, annotated with each pack's `enabled` flag for the calling
// tenant's subscription. The dashboard renders its nav from that response so
// adding/removing/relabelling a module is a one-file change on the backend
// (modules.json) with no frontend edit — the constants above are the offline
// fallback used only when /modules can't be reached.

export type ModuleView = { key: string; label: string; icon: string };

export type ModulePack = {
  id: string;
  label: string;
  description: string;
  tagline: string;
  color: string;
  gated: boolean;
  plans: string[];
  enabled: boolean;
  views: ModuleView[];
};

export type ModulesResponse = {
  tenant: string;
  plan: string;
  enabled_modules: string[];
  packs: ModulePack[];
  plan_bundles?: Record<string, string[]>;
};

// Flatten the manifest packs into the nav list (pack order preserved). A view's
// `module` is its pack id, so role/plan gating below keeps working unchanged.
export function navItemsFromPacks(packs: ModulePack[]): NavItem[] {
  return packs.flatMap((p) =>
    (p.views || []).map((v) => ({
      key: v.key,
      label: v.label,
      icon: v.icon,
      module: p.id,
    }))
  );
}

// The sidebar group headers, from the manifest packs.
export function catalogFromPacks(packs: ModulePack[]): ModuleInfo[] {
  return packs.map((p) => ({
    key: p.id,
    label: p.label,
    description: p.description,
    tagline: p.tagline,
    color: p.color,
  }));
}

// The module keys a tenant can use: every pack the subscription enables, plus
// the never-gated packs (core basics + account admin) — mirrors the server-side
// plan-gate's always-open set so the UI and the API agree.
export function enabledModulesFromPacks(packs: ModulePack[]): ModuleKey[] {
  const on = packs
    .filter((p) => p.enabled || !p.gated)
    .map((p) => p.id);
  return withAlwaysOpen(on);
}

// view→module and view enablement resolved against a supplied nav list (the live
// manifest one, or the static fallback). Using the live list means a module the
// manifest adds resolves correctly even before this file is updated.
export function getViewModuleIn(viewKey: string, navItems: NavItem[]): ModuleKey {
  return navItems.find((n) => n.key === viewKey)?.module ?? "core";
}

export function isViewEnabledIn(
  viewKey: string,
  enabledModules: ModuleKey[],
  navItems: NavItem[]
): boolean {
  const item = navItems.find((n) => n.key === viewKey);
  if (!item) return false;
  return enabledModules.includes(item.module);
}
