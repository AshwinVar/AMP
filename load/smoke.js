/**
 * Smoke: one user, one pass, every scripted endpoint must answer 2xx.
 *
 * WHY THIS SCRIPT EXISTS
 * The other two scripts generate load. This one generates trust in them. A load
 * result is worthless if half the requests were 404ing on a renamed route or
 * 403ing on a plan-gated pack — the p95 would look wonderful and mean nothing.
 * So before anyone quotes a number from dashboard-poll.js or read-models.js,
 * this proves that every path those scripts fire actually exists, that the
 * credentials in use can reach it, and that the target instance is alive.
 *
 * It is also the only one of the three that is safe and sensible to run in CI:
 * one VU, one iteration, no sustained pressure.
 *
 *   *** Defaults to localhost. See load/config.js for the guard. ***
 *
 * Run:
 *   LOAD_USER=admin LOAD_PASS=... k6 run load/smoke.js
 */
import { check, fail } from "k6";

import {
  DASHBOARD_BLOCKING,
  DASHBOARD_OPTIONAL,
  DASHBOARD_ROUND,
  PARAMETERISED_READ_MODELS,
  PUBLIC,
  READ_MODELS,
} from "./endpoints.js";
import { SMOKE_THRESHOLDS } from "./thresholds.js";
import { announce, assertTargetIsSafe, get, login } from "./config.js";

export const options = {
  vus: 1,
  iterations: 1,
  thresholds: SMOKE_THRESHOLDS,
};

const AUTHENTICATED = DASHBOARD_ROUND.concat(READ_MODELS).concat(
  PARAMETERISED_READ_MODELS,
);

export function setup() {
  assertTargetIsSafe();
  announce("smoke", {
    endpoints: AUTHENTICATED.length + PUBLIC.length,
    dashboard_round: DASHBOARD_ROUND.length,
  });
  return login();
}

/**
 * Explain a non-2xx in terms of the thing that actually causes it here, rather
 * than making the reader go and find it. All three of these are configuration
 * of the RUN, not defects in the application, and all three have bitten
 * somebody's first load test somewhere.
 */
function diagnose(status) {
  if (status === 401) {
    return "401 — the token was rejected. Expired mid-run, or SECRET_KEY differs from the one that signed it.";
  }
  if (status === 403) {
    return (
      "403 — either the plan gate (backend/plan_gate.py: the tenant's licence " +
      "does not include this module pack) or a role gate (e.g. /users and the " +
      "SaaS registry require Admin). Run as an Admin on a fully licensed tenant."
    );
  }
  if (status === 429) {
    return "429 — throttled (backend/http_security.py). Only /login, /register, /auth/change-password and the AI routes are limited.";
  }
  if (status === 0) {
    return "0 — no response at all. The target is not listening, or the request timed out.";
  }
  return `${status} — unexpected.`;
}

function walk(paths, token, label) {
  const failures = [];
  for (const path of paths) {
    const res = get(path, token);
    const ok = check(res, {
      "status is 2xx": (r) => r.status >= 200 && r.status < 300,
    });
    if (!ok) failures.push(`  ${path}  ->  ${diagnose(res.status)}`);
  }
  if (failures.length) {
    console.error(`${label}: ${failures.length}/${paths.length} failed\n${failures.join("\n")}`);
  } else {
    console.log(`${label}: ${paths.length}/${paths.length} returned 2xx`);
  }
  return failures.length;
}

export default function (auth) {
  let failed = 0;

  // Public first: if /health is not 200 the rest of the walk is noise.
  failed += walk(PUBLIC, auth.token, "public");

  // The blocking three are called out separately because a failure here is a
  // blank dashboard, not a missing card — worth seeing at a glance.
  failed += walk(DASHBOARD_BLOCKING, auth.token, "dashboard blocking (3)");
  failed += walk(DASHBOARD_OPTIONAL, auth.token, "dashboard optional (43)");
  failed += walk(READ_MODELS, auth.token, "read-models");
  failed += walk(PARAMETERISED_READ_MODELS, auth.token, "read-model drill-downs");

  console.log(
    `smoke complete as role=${auth.role} tenant=${auth.tenant}: ` +
      `${AUTHENTICATED.length + PUBLIC.length} endpoints, ${failed} failed`,
  );

  // The `checks` threshold already fails the run and the exit code, but failing
  // loudly here puts the reason at the bottom of the output where people look.
  if (failed > 0) {
    fail(`${failed} endpoint(s) did not return 2xx — see the lines above.`);
  }
}
