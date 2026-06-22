export type ModuleKey = "core" | "operations" | "factory" | "intelligence" | "admin";
export type PlanName = "starter" | "growth" | "enterprise" | "demo";

export type NavItem = {
  key: string;
  label: string;
  icon: string;
  module: ModuleKey;
};

export const NAV_ITEMS: NavItem[] = [
  { key: "overview",       label: "Overview",          icon: "⌂", module: "core" },
  { key: "machines",       label: "Machines",           icon: "▦", module: "core" },
  { key: "downtime",       label: "Downtime",           icon: "◷", module: "core" },
  { key: "shifts",         label: "Shifts",             icon: "◴", module: "core" },
  { key: "analytics",      label: "Analytics",          icon: "▧", module: "core" },
  { key: "timeline",       label: "Timeline",           icon: "↔", module: "core" },
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
  { key: "iot",            label: "IoT Command",        icon: "◉", module: "intelligence" },
  { key: "ai",             label: "AI Insights",        icon: "✦", module: "intelligence" },
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

export function getEnabledModules(plan: PlanName): ModuleKey[] {
  return PLAN_MODULES[plan] ?? ["core"];
}

export function isViewEnabled(viewKey: string, enabledModules: ModuleKey[]): boolean {
  const item = NAV_ITEMS.find((n) => n.key === viewKey);
  if (!item) return false;
  return enabledModules.includes(item.module);
}

export function getViewModule(viewKey: string): ModuleKey {
  return NAV_ITEMS.find((n) => n.key === viewKey)?.module ?? "core";
}
