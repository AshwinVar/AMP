/**
 * The endpoint catalogues the load scripts fire at.
 *
 * WHY THIS FILE EXISTS
 * The load profile that matters is not a synthetic one — it is exactly what an
 * open dashboard tab does. If the list lives inside each script it will drift
 * from the frontend the first time someone adds a card, and the harness will
 * quietly stop measuring the real thing. One catalogue, transcribed from the
 * source, with the source named, so drift is a diff rather than a mystery.
 *
 * SOURCES OF TRUTH — re-check these when the numbers stop looking right:
 *   DASHBOARD_BLOCKING / DASHBOARD_OPTIONAL
 *       frontend/app/dashboard/page.tsx, fetchAll(). The blocking three are the
 *       Promise.all; the other 43 are the Promise.allSettled, in source order.
 *       Polled every 3000ms by usePolling (page.tsx: usePolling(fetchAll, 3000)).
 *   READ_MODELS
 *       backend/read_model_routes.py, plus the read-model endpoints the snapshot
 *       cards fetch on a 30s interval of their own (grep setInterval in
 *       frontend/components).
 *
 * Every path below was verified against the live FastAPI route table, not
 * against the frontend alone — see docs/PERFORMANCE.md, "Verifying the paths".
 */

/**
 * The three the dashboard awaits before it renders anything. A failure here is
 * a blank screen, not a missing card, which is why they are separated: their
 * latency is the user-visible one.
 */
export const DASHBOARD_BLOCKING = [
  "/machines",
  "/downtime-logs",
  "/shifts",
];

/**
 * The 43 issued as one Promise.allSettled. The dashboard tolerates individual
 * failures here (a settled rejection just leaves that card's state untouched),
 * so a non-2xx is a degraded screen rather than a broken one — but it is still
 * a request the server paid for, and it still counts against the 3s round.
 */
export const DASHBOARD_OPTIONAL = [
  "/analytics/machine-timeline",
  "/analytics/machine-state-summary",
  "/work-orders",
  "/analytics/work-orders",
  "/analytics/predictive-maintenance",
  "/production-plans",
  "/analytics/production-plans",
  "/escalations",
  "/analytics/escalations",
  "/inventory/items",
  "/inventory/transactions",
  "/analytics/inventory",
  "/quality/inspections",
  "/analytics/quality",
  "/analytics/executive-oee",
  "/factory-layout/nodes",
  "/analytics/factory-command-center",
  "/customer-orders",
  "/analytics/customer-orders",
  "/suppliers",
  "/purchase-orders",
  "/analytics/purchasing",
  "/documents",
  "/analytics/documents",
  "/maintenance/tasks",
  "/analytics/maintenance",
  "/production-schedules",
  "/analytics/production-schedules",
  "/iot/telemetry",
  "/analytics/iot-command",
  "/ai/recommendations",
  "/analytics/ai-insights",
  "/saas/tenants",
  "/analytics/saas",
  "/cost-records",
  "/analytics/costing",
  "/operator/executions",
  "/analytics/operator-terminal",
  "/audit-logs",
  "/notifications",
  "/reports",
  "/analytics/system-health",
  "/analytics/final-executive-summary",
];

/** One poll round, in the order the browser issues it. */
export const DASHBOARD_ROUND = DASHBOARD_BLOCKING.concat(DASHBOARD_OPTIONAL);

/** The interval usePolling ticks at (frontend/lib/usePolling.ts, called with
 *  3000 from page.tsx). This is the budget a whole round has to fit inside
 *  before the next tick starts getting skipped. */
export const DASHBOARD_INTERVAL_MS = 3000;

/**
 * The composite read-models — the ones that fan out across several pillars to
 * build one response. These are the plausible saturation points, and the ones
 * the optimisation commits touched, so they get their own ramp.
 *
 * The ordering is roughly "most composed first": /weekly-report builds on the
 * scorecard, cost, delivery and briefing models; /briefing itself ranks alerts
 * across every pillar; /handover composes the pillar models again; /copilot/digest
 * summarises the lot. If a single worker is going to fall over, it falls over
 * here.
 */
export const READ_MODELS_COMPOSITE = [
  "/weekly-report",
  "/briefing",
  "/handover",
  "/copilot/digest",
  "/scorecard",
  "/mission-control/pulse",
  "/machine-health",
  "/twin-overlay",
];

/**
 * The pillar read-models each snapshot card polls on its own 30s timer. Lighter
 * individually, but there are a lot of them and they run concurrently with the
 * 3s fetchAll round, so they are part of the same steady-state load.
 */
export const READ_MODELS_PILLAR = [
  "/oee-summary",
  "/oee-trend",
  "/losses-summary",
  "/recovery-summary",
  "/downtime-summary",
  "/downtime-trend",
  "/quality-summary",
  "/quality-trend",
  "/production-summary",
  "/flow-summary",
  "/shift-summary",
  "/wip-aging",
  "/inventory-summary",
  "/coverage-summary",
  "/stock-health",
  "/stock-accuracy",
  "/supplier-performance",
  "/supply-summary",
  "/delivery-summary",
  "/schedule-summary",
  "/schedule-load",
  "/cost-summary",
  "/cost-trend",
  "/maintenance-summary",
  "/maintenance-execution",
  "/maintenance-forecast",
  "/escalation-summary",
  "/reliability-summary",
  "/connectivity-summary",
  "/compliance-summary",
  "/operator-summary",
  "/insights",
];

/** Everything the read-model ramp exercises. */
export const READ_MODELS = READ_MODELS_COMPOSITE.concat(READ_MODELS_PILLAR);

/**
 * Read-models that need a query parameter. Kept apart from the parameterless
 * ones because a drill-down's cost depends on the argument, so a single fixed
 * value is a sample rather than a representative load. The defaults below are
 * only reached in smoke.js, where the point is "does it answer at all" — every
 * one of these handles an unknown key by returning an empty projection rather
 * than erroring, so the smoke run stays green on a fresh database.
 */
export const PARAMETERISED_READ_MODELS = [
  "/downtime-reason?reason=Material%20Shortage",
  "/quality-defect?category=Cosmetic",
  "/inventory-part?item_code=UNKNOWN",
  "/delivery-customer?customer=UNKNOWN",
  "/supply-supplier?supplier=UNKNOWN",
  "/schedule-shift?shift=Shift%20A",
  "/work-order-trace?work_order_no=UNKNOWN",
  "/operator-detail?operator=UNKNOWN",
  "/reliability-machine?machine_id=1",
  "/connectivity-machine?machine_id=1",
  "/search?q=machine",
];

/** Public, unauthenticated. /health is what an uptime monitor hits and it does
 *  a real SELECT 1, so it is the cheapest honest liveness probe in the app
 *  (backend/platform_routes.py). */
export const PUBLIC = ["/", "/health"];
