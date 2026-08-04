/**
 * Latency and error budgets, and the per-endpoint threshold trick that makes
 * k6 print a breakdown.
 *
 * WHY THIS FILE EXISTS
 * A load run with no thresholds always "passes" — it prints numbers and exits
 * zero, so it can never be a CI gate and it can never tell you that something
 * regressed. Thresholds turn a measurement into an assertion.
 *
 * READ THIS BEFORE TRUSTING THE NUMBERS BELOW
 * These are DERIVED BUDGETS, not measured baselines. Nothing in this repository
 * has ever been load tested, so there is no observed p95 to set them from. They
 * come from arithmetic on the architecture (see DASHBOARD_REQUEST_BUDGET_MS) and
 * are deliberately stated rather than fudged. The first real run may well blow
 * straight through them — that is the point. Record the run in the baseline
 * table in docs/PERFORMANCE.md, then come back and either tighten these to
 * protect the measured behaviour or raise them with a written reason.
 */
import { DASHBOARD_INTERVAL_MS, DASHBOARD_ROUND } from "./endpoints.js";

/**
 * The per-request budget implied by the dashboard's own design, not by taste.
 *
 * A round is 46 requests. A browser opens at most ~6 concurrent connections per
 * origin, so a round is about ceil(46/6) = 8 sequential waves. The round has to
 * finish inside the 3000ms poll interval or usePolling starts skipping ticks.
 *   3000 / 8 = 375ms per wave, and a wave is as slow as its slowest member.
 * Rounding down to 300ms leaves headroom for the wave being uneven and for the
 * ~30s snapshot-card polls landing on top of the round.
 *
 * If this arithmetic ever stops matching reality — a different interval, a
 * different round size, HTTP/2 multiplexing removing the 6-connection cap —
 * change it here rather than sprinkling magic numbers through the scripts.
 */
export const DASHBOARD_WAVES = Math.ceil(DASHBOARD_ROUND.length / 6);
export const DASHBOARD_REQUEST_BUDGET_MS = Math.floor(
  (DASHBOARD_INTERVAL_MS / DASHBOARD_WAVES) * 0.8,
);

/** Composite read-models legitimately do more work than a list endpoint: they
 *  compose several pillar projections into one response. They are still on the
 *  30s card timer rather than the 3s round, so the budget is looser — but a
 *  read-model slower than this is a page that visibly fills in late. */
export const READ_MODEL_BUDGET_MS = 1500;

/** Error budget. Deliberately near-zero rather than "a few percent": every
 *  endpoint in these scripts is a plain read against a running instance, so a
 *  non-2xx is either a bug, a plan-gate/role misconfiguration in the run, or the
 *  server falling over — none of which should be absorbed silently. */
export const ERROR_RATE_BUDGET = 0.01;

/**
 * Per-endpoint thresholds.
 *
 * These exist as much for OUTPUT as for enforcement. k6's end-of-test summary
 * aggregates http_req_duration across the whole run; the only supported way to
 * get a per-endpoint row printed in the terminal is to declare a threshold on
 * the tagged sub-metric. So every path gets one, generously budgeted, and the
 * summary becomes the per-endpoint table you actually wanted.
 *
 * abortOnFail is left off on purpose: one slow endpoint should colour its own
 * row red and fail the run at the end, not tear down a run you are still
 * collecting data from.
 */
export function perEndpointThresholds(paths, budgetMs) {
  const out = {};
  for (const path of paths) {
    out[`http_req_duration{name:${path}}`] = [`p(95)<${budgetMs}`];
  }
  return out;
}

/** Budgets shared by every script, so a regression in the basics fails
 *  whichever script you happened to run. */
export const BASE_THRESHOLDS = {
  http_req_failed: [`rate<${ERROR_RATE_BUDGET}`],
  checks: ["rate>0.99"],
};

/**
 * The dashboard-poll budget.
 *
 * `dashboard_round_duration` is the metric that matters and the one no other
 * tool would have given you: the wall-clock time for one complete 46-request
 * round. Once its p95 crosses the 3000ms interval, usePolling begins skipping
 * ticks — the tab is no longer refreshing at 3s, it is refreshing at whatever
 * the server can manage, and the operator is reading stale numbers without any
 * indication that they are stale. That is the failure this whole harness exists
 * to detect, so it gets a hard threshold at exactly the interval.
 */
export function dashboardThresholds(perEndpoint) {
  const thresholds = Object.assign({}, BASE_THRESHOLDS, {
    dashboard_round_duration: [`p(95)<${DASHBOARD_INTERVAL_MS}`],
    dashboard_round_overran: ["rate<0.05"],
    // The blocking three gate first paint, so they carry the tighter budget in
    // their own right. `phase`, not `group` — `group` is k6's own reserved tag.
    "http_req_duration{phase:blocking}": [`p(95)<${DASHBOARD_REQUEST_BUDGET_MS}`],
    http_req_duration: [`p(95)<${DASHBOARD_REQUEST_BUDGET_MS}`],
  });
  if (perEndpoint) {
    // A single endpoint inside a 6-wide wave may legitimately take longer than
    // the per-wave budget without breaking the round, so the per-endpoint rows
    // are budgeted against the whole interval. They are here to show you WHICH
    // endpoint is heavy, not to fail the run twice for the same problem.
    Object.assign(
      thresholds,
      perEndpointThresholds(DASHBOARD_ROUND, DASHBOARD_INTERVAL_MS),
    );
  }
  return thresholds;
}

export function readModelThresholds(paths, perEndpoint) {
  const thresholds = Object.assign({}, BASE_THRESHOLDS, {
    http_req_duration: [`p(95)<${READ_MODEL_BUDGET_MS}`],
  });
  if (perEndpoint) {
    Object.assign(thresholds, perEndpointThresholds(paths, READ_MODEL_BUDGET_MS));
  }
  return thresholds;
}

/** Smoke is a correctness gate, not a performance one: nothing may fail, and
 *  the latency bar is loose enough that a cold, unwarmed instance still passes.
 *  A smoke run that fails on latency would train people to ignore it. */
export const SMOKE_THRESHOLDS = {
  http_req_failed: ["rate==0"],
  checks: ["rate==1"],
  http_req_duration: ["p(95)<2000"],
};
