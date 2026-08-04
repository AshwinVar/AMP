/**
 * The mocked backend.
 *
 * The dashboard fans out to roughly 47 endpoints per poll round plus whatever
 * the visible cards fetch for themselves, so a browser test that needs a real
 * API needs a real database with real seeded rows — which is exactly why this
 * repo has never had one. Intercepting at the network boundary with
 * `page.route()` gives the specs the two things a journey test actually needs:
 * it runs in CI with nothing but Node, and the numbers on screen are known, so
 * assertions can be about VALUES ("2 running machines") rather than about the
 * page merely not being blank.
 *
 * Design rules, both learned from what this app does when data is missing:
 *
 *   - An endpoint with no fixture answers 404, never 200-with-empty-JSON. Every
 *     card here loads inside try/catch and renders nothing on failure (the #383
 *     empty-state-on-error work), so a 404 is inert. A blanket `{}` or `[]` is
 *     NOT: `OeeSnapshot` reads `s.plant.has_data` and would throw on `{}`,
 *     taking the whole page down and making an unrelated spec fail for a
 *     reason nothing in its name suggests.
 *   - 404, not 401. `lib/api.ts handleUnauthorized` treats a 401 on an expired
 *     token as "session over" and navigates to /login; an unfixtured endpoint
 *     must not be able to log the test out by accident.
 *
 * Run with E2E_DEBUG_API=1 and every unfixtured path is named once, so a spec
 * that quietly lost its data has somewhere obvious to look.
 */

import type { Page, Request, Route } from "@playwright/test";

import { DEFAULT_API_BASE, LIVE_API, MOCK_API_BASE } from "./env";

export type MockResponse = { status?: number; json?: unknown; body?: string };

/**
 * Either a plain fixture body — served as 200 — or a function returning an
 * explicit response. The two are told apart by `typeof`, so a fixture that
 * happens to have a `status` field is still just data; use `respondWith` when
 * you mean a status code.
 */
export type MockHandler = unknown;

/**
 * Path (no query string) to fixture. The key "*" overrides every path, which is
 * how the expired-session spec makes the whole API answer 401 at once.
 */
export type MockTable = Record<string, MockHandler>;

/** An explicit status/body reply, for the cases where 200 is not the point. */
export function respondWith(status: number, json: unknown) {
  return () => ({ status, json });
}

// ── Credentials the mocked /login accepts ────────────────────────────
export const MOCK_USERNAME = "alice";
export const MOCK_PASSWORD = "correct-horse-battery";

// ── Fixture data ─────────────────────────────────────────────────────
// Naive UTC, no zone suffix — the shape the backend actually sends
// (`datetime.isoformat()` on a naive column, see lib/apiDate.ts).
function apiTimestamp(minutesAgo: number): string {
  return new Date(Date.now() - minutesAgo * 60_000).toISOString().replace("Z", "");
}

/**
 * Chosen so every derived KPI is a whole number a human can check by hand:
 * 2 Running, 1 Breakdown, utilisation (82 + 74 + 12) / 3 = 56.
 */
export const MACHINES = [
  { id: 1, name: "SMT Line 1", status: "Running", utilization: 82, downtime: "12 min" },
  { id: 2, name: "Reflow Oven", status: "Running", utilization: 74, downtime: "0 min" },
  { id: 3, name: "AOI Station", status: "Breakdown", utilization: 12, downtime: "96 min" },
];

/** 3 events, 45 + 30 + 15 = 90 minutes, top reason "Material Shortage" (2 of 3). */
export const DOWNTIME_LOGS = [
  { id: 1, machine_id: 3, reason: "Material Shortage", duration: "45 min", notes: "Feeder starved" },
  { id: 2, machine_id: 1, reason: "Material Shortage", duration: "30 min", notes: "" },
  { id: 3, machine_id: 2, reason: "Tool Change", duration: "15 min", notes: "" },
];

export const SHIFTS = [
  { id: 1, shift_name: "Shift A", target_output: 500, actual_output: 460 },
  { id: 2, shift_name: "Shift B", target_output: 500, actual_output: 420 },
];

/**
 * A faithful copy of backend/modules.json as GET /modules serves it (packs
 * annotated with `enabled`). Held as a literal rather than imported from
 * lib/modules so that changing the app's compiled-in fallback cannot silently
 * change what the "server" in these tests replies.
 */
export const MODULES_MANIFEST = {
  tenant: "DEFAULT",
  plan: "enterprise",
  enabled_modules: ["core", "operations", "factory", "intelligence", "admin"],
  packs: [
    {
      id: "core",
      label: "Core MES",
      description: "Real-time machine monitoring, downtime tracking, OEE, and shift performance.",
      tagline: "Included in every plan",
      color: "blue",
      gated: false,
      plans: ["starter", "growth", "enterprise", "demo"],
      enabled: true,
      views: [
        { key: "mission", label: "Mission Control", icon: "❖" },
        { key: "overview", label: "Overview", icon: "⌂" },
        { key: "machines", label: "Machines", icon: "▦" },
        { key: "downtime", label: "Downtime", icon: "◷" },
        { key: "shifts", label: "Shifts", icon: "◴" },
        { key: "analytics", label: "Analytics", icon: "▧" },
        { key: "trends", label: "Trends", icon: "▨" },
        { key: "timeline", label: "Timeline", icon: "↔" },
      ],
    },
    {
      id: "operations",
      label: "Operations Pack",
      description: "Work orders, production planning, scheduling, operator terminal, and order dispatch.",
      tagline: "For production teams",
      color: "green",
      gated: true,
      plans: ["growth", "enterprise", "demo"],
      enabled: true,
      views: [
        { key: "workorders", label: "Work Orders", icon: "▣" },
        { key: "planning", label: "Production Plan", icon: "▤" },
        { key: "scheduling", label: "Scheduling", icon: "◫" },
        { key: "operator", label: "Operator Terminal", icon: "▶" },
        { key: "orders", label: "Orders & Dispatch", icon: "⇄" },
      ],
    },
    {
      id: "factory",
      label: "Factory Pack",
      description: "Predictive maintenance AI, CMMS, quality inspections, inventory, purchasing, and digital twin.",
      tagline: "Full shopfloor visibility",
      color: "purple",
      gated: true,
      plans: ["growth", "enterprise", "demo"],
      enabled: true,
      views: [
        { key: "maintenance_ai", label: "Maintenance AI", icon: "◇" },
        { key: "cmms", label: "CMMS", icon: "⚙" },
        { key: "quality", label: "Quality", icon: "✓" },
        { key: "inventory", label: "Inventory", icon: "▥" },
        { key: "purchasing", label: "Purchasing", icon: "◈" },
        { key: "digitaltwin", label: "Digital Twin", icon: "◎" },
        { key: "machinehealth", label: "Machine Health", icon: "♥" },
      ],
    },
    {
      id: "intelligence",
      label: "Intelligence Pack",
      description: "IoT command center, AI-driven insights, the AI Copilot, executive OEE dashboards, and smart escalations.",
      tagline: "Data-driven decisions",
      color: "amber",
      gated: true,
      plans: ["enterprise", "demo"],
      enabled: true,
      views: [
        { key: "iot", label: "IoT Command", icon: "◉" },
        { key: "connectivity", label: "Connectivity", icon: "⇄" },
        { key: "ai", label: "AI Insights", icon: "✦" },
        { key: "copilot", label: "AI Copilot", icon: "✸" },
        { key: "inbox", label: "Inbox", icon: "✉" },
        { key: "agentactivity", label: "Agent Activity", icon: "⊙" },
        { key: "roi", label: "AI Impact", icon: "◊" },
        { key: "executive", label: "Executive OEE", icon: "▰" },
        { key: "escalations", label: "Escalations", icon: "!" },
        { key: "notifications", label: "Notifications", icon: "●" },
      ],
    },
    {
      id: "admin",
      label: "Admin Pack",
      description: "Compliance documents, cost tracking, SaaS tenant management, and enterprise reporting.",
      tagline: "For management & compliance",
      color: "red",
      gated: false,
      plans: ["enterprise", "demo"],
      enabled: true,
      views: [
        { key: "documents", label: "Documents", icon: "▱" },
        { key: "saas", label: "SaaS Admin", icon: "◌" },
        { key: "users", label: "User Management", icon: "◔" },
        { key: "costing", label: "Costing", icon: "£" },
        { key: "enterprise", label: "Enterprise Polish", icon: "◆" },
      ],
    },
  ],
};

export const INSIGHTS = [
  {
    source: "recommendation",
    kind: "predictive_maintenance",
    severity: "Critical",
    title: "AOI Station is trending to failure",
    message: "Three breakdowns in seven days and rising cycle variance.",
    occurred_at: apiTimestamp(6),
    related_machine_id: 3,
    ref_id: 41,
  },
  {
    source: "action",
    kind: "reschedule",
    severity: "High",
    title: "Move WO-1042 to Reflow Oven",
    message: "SMT Line 1 is the constraint for the next two shifts.",
    occurred_at: apiTimestamp(22),
    related_machine_id: 1,
    ref_id: 12,
  },
  {
    source: "recommendation",
    kind: "quality",
    severity: "Medium",
    title: "Solder bridging is concentrated on one feeder",
    message: "Nine of eleven rejects this week came from feeder 4.",
    occurred_at: apiTimestamp(95),
    related_machine_id: 1,
    ref_id: 42,
  },
];

export const PULSE = {
  fleet: {
    machines: 3,
    avg_health: 71,
    needs_attention: 1,
    worst: { machine_id: 3, name: "AOI Station", health_score: 38, health_band: "At risk" },
  },
  agents: { agents_active: 4, actions_7d: 22, auto_rate: 64, awaiting_you: 2 },
  headline: "One machine needs attention before the next shift.",
};

export const SCORECARD = {
  has_data: true,
  kpis: [
    { key: "oee", label: "OEE", value: 68.4, unit: "%", tone: "warn", delta: null, delta_tone: null },
    { key: "good_rate", label: "Good rate", value: 97.2, unit: "%", tone: "good", delta: null, delta_tone: null },
    { key: "on_time", label: "On time", value: 91, unit: "%", tone: "good", delta: null, delta_tone: null },
  ],
};

const OEE_BLOCK = { oee: 64, availability: 82, performance: 86, quality: 91, has_data: true };

export const MACHINE_TWINS = [
  {
    machine_id: 1,
    name: "SMT Line 1",
    line: "SMT",
    status: "Running",
    utilization: 82,
    downtime: "12 min",
    health_score: 78,
    health_band: "Watch",
    risk_score: 24,
    risk_level: "Low",
    top_reason: "One short material stop this week.",
    open_maintenance_tasks: 1,
    pending_agent_actions: 1,
    oee: OEE_BLOCK,
  },
  {
    machine_id: 3,
    name: "AOI Station",
    line: "IC",
    status: "Breakdown",
    utilization: 12,
    downtime: "96 min",
    health_score: 38,
    health_band: "At risk",
    risk_score: 71,
    risk_level: "High",
    top_reason: "Three breakdowns in seven days.",
    open_maintenance_tasks: 2,
    pending_agent_actions: 1,
    oee: { ...OEE_BLOCK, oee: 31, availability: 44 },
  },
];

/**
 * The drill-down read-model, built for whichever machine was clicked so the
 * spec never has to know which card it opened. Two open actions are included
 * on purpose: the focus-trap assertion is only meaningful if the drawer has
 * several focusable controls to walk through.
 */
export function machineDetail(machineId: number) {
  const twin = MACHINE_TWINS.find((t) => t.machine_id === machineId) ?? MACHINE_TWINS[0];
  return {
    machine_id: machineId,
    name: twin.name,
    line: twin.line,
    status: twin.status,
    utilization: twin.utilization,
    downtime: twin.downtime,
    health_score: twin.health_score,
    health_band: twin.health_band,
    risk_score: twin.risk_score,
    risk_level: twin.risk_level,
    oee: twin.oee,
    risk_factors: ["Breakdowns above fleet median", "Preventive task overdue by 4 days"],
    downtime_7d: [
      { date: "2026-07-27", count: 1 },
      { date: "2026-07-28", count: 0 },
      { date: "2026-07-29", count: 2 },
      { date: "2026-07-30", count: 0 },
      { date: "2026-07-31", count: 1 },
      { date: "2026-08-01", count: 0 },
      { date: "2026-08-02", count: 1 },
    ],
    production_7d: {
      good: 4820,
      total: 5000,
      good_rate: 96.4,
      daily: [
        { date: "2026-07-27", count: 700 },
        { date: "2026-07-28", count: 820 },
        { date: "2026-07-29", count: 640 },
        { date: "2026-07-30", count: 760 },
        { date: "2026-07-31", count: 700 },
        { date: "2026-08-01", count: 690 },
        { date: "2026-08-02", count: 690 },
      ],
    },
    quality: {
      inspections: 12,
      inspected: 480,
      passed: 468,
      failed: 12,
      fail_rate: 2.5,
      top_defects: [
        { category: "Solder bridging", count: 7 },
        { category: "Misalignment", count: 5 },
      ],
    },
    open_actions: [
      {
        id: 12,
        agent: "maintenance",
        action_type: "raise_task",
        summary: "Raise a preventive task for the pick head",
        severity: "High",
        created_at: apiTimestamp(40),
      },
      {
        id: 13,
        agent: "quality",
        action_type: "hold_lot",
        summary: "Hold lot L-7781 pending re-inspection",
        severity: "Medium",
        created_at: apiTimestamp(180),
      },
    ],
    timeline: [
      { kind: "downtime", at: apiTimestamp(120), title: "Material Shortage", detail: "45 min", status: "Closed" },
      { kind: "task", at: apiTimestamp(600), title: "Preventive service", detail: "Lubricate rails", status: "Open" },
    ],
  };
}

/** Everything a mocked run answers by default. Specs override per test. */
export function defaultMockTable(): MockTable {
  return {
    "/login": (request: Request) => {
      const body = (request.postDataJSON() ?? {}) as { username?: string; password?: string };
      if (body.username === MOCK_USERNAME && body.password === MOCK_PASSWORD) {
        return {
          status: 200,
          json: {
            // Minted here rather than imported so the login path is exercised
            // exactly as the app receives it: a token it has never seen before.
            access_token: mintedLoginToken(body.username),
            role: "Admin",
            tenant: "DEFAULT",
          },
        };
      }
      return { status: 401, json: { detail: "Invalid username or password" } };
    },
    "/modules": MODULES_MANIFEST,
    "/tenant-config": {
      enabled_modules: ["core", "operations", "factory", "intelligence", "admin"],
      brand_name: "AMP",
      brand_color: "#3b82f6",
    },
    "/machines": MACHINES,
    "/downtime-logs": DOWNTIME_LOGS,
    "/shifts": SHIFTS,
    "/insights": INSIGHTS,
    "/mission-control/pulse": PULSE,
    "/scorecard": SCORECARD,
    "/machine-health": MACHINE_TWINS,
    "/users": [
      { id: 1, username: "alice", role: "Admin" },
      { id: 2, username: "bob", role: "Operator" },
    ],
  };
}

// Kept out of session.ts so that file stays about planting a session; this is
// the server's half of the login exchange.
function mintedLoginToken(username: string): string {
  const now = Math.floor(Date.now() / 1000);
  const seg = (value: object) => Buffer.from(JSON.stringify(value), "utf8").toString("base64");
  return [
    seg({ alg: "HS256", typ: "JWT" }),
    seg({ sub: username, role: "Admin", tenant: "DEFAULT", exp: now + 3600 }),
    "e2e-signature-never-verified",
  ].join(".");
}

/** Path patterns, tried after an exact match fails. */
const PATTERNS: [RegExp, MockHandler][] = [
  [/^\/machine-health\/\d+$/, (request: Request) => {
    const id = Number(new URL(request.url()).pathname.split("/").pop());
    return { status: 200, json: machineDetail(id) };
  }],
];

const JSON_HEADERS: Record<string, string> = {
  "content-type": "application/json",
  // Harmless same-origin, load-bearing if a run ends up talking to :8000.
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "*",
  "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
};

/**
 * The API path a request is asking for, or null when it is not API traffic at
 * all (a page, a chunk, an image). Fixtures are keyed on the path alone —
 * `apiGet` appends a `t=` cache-buster to every call, and `url.pathname` drops
 * it for us.
 */
function apiPathOf(url: URL): string | null {
  const mockPrefix = new URL(MOCK_API_BASE).pathname;
  if (url.pathname === mockPrefix) return "/";
  if (url.pathname.startsWith(`${mockPrefix}/`)) return url.pathname.slice(mockPrefix.length);
  if (url.origin === new URL(DEFAULT_API_BASE).origin) return url.pathname;
  return null;
}

export async function installMockApi(page: Page, overrides: MockTable = {}): Promise<void> {
  // Nothing is intercepted in a live run: the app talks to the real backend.
  // A no-op rather than a throw, so specs can call this unconditionally.
  if (LIVE_API) return;

  // A spec whose card renders nothing usually means a fixture is missing, and
  // the page gives no clue which. E2E_DEBUG_API=1 names them.
  const debug = process.env.E2E_DEBUG_API === "1";
  const reported = new Set<string>();

  const table: MockTable = { ...defaultMockTable(), ...overrides };

  // The live feed. Left unrouted it would fail to connect and lib/live.ts would
  // retry on a backoff for the whole test; answering it keeps the socket open
  // and silent.
  await page.routeWebSocket(/\/ws\/live/, () => {
    // No connectToServer() call: the socket is mocked and never reaches a
    // server. The dashboard only needs it to open.
  });

  await page.route(
    (url) => apiPathOf(url) !== null,
    async (route: Route) => {
      const request = route.request();
      const path = apiPathOf(new URL(request.url()));

      if (path === null) {
        await route.continue();
        return;
      }

      // A preflight only appears on the cross-origin fallback path; answering
      // it costs nothing and stops that path from failing before the mock runs.
      if (request.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: JSON_HEADERS, body: "" });
        return;
      }

      const handler = table["*"] ?? table[path] ?? PATTERNS.find(([re]) => re.test(path))?.[1];

      if (handler === undefined) {
        const key = `${request.method()} ${path}`;
        if (debug && !reported.has(key)) {
          reported.add(key);
          console.log(`[e2e] no fixture for ${key} - answering 404`);
        }
        await route.fulfill({
          status: 404,
          headers: JSON_HEADERS,
          body: JSON.stringify({ detail: `No e2e fixture for ${path}` }),
        });
        return;
      }

      const result =
        typeof handler === "function"
          ? await (handler as (r: Request) => MockResponse | Promise<MockResponse>)(request)
          : ({ status: 200, json: handler } as MockResponse);

      await route.fulfill({
        status: result.status ?? 200,
        headers: JSON_HEADERS,
        body: result.body ?? JSON.stringify(result.json ?? null),
      });
    },
  );
}
