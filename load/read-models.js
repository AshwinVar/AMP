/**
 * Ramp against the heaviest read-models until something gives.
 *
 * WHY THIS SCRIPT EXISTS
 * dashboard-poll.js answers "does the product hold up at N tabs?". This one
 * answers the different question the architecture forces: "where does it break
 * first, and at what concurrency?" The read-models are the obvious candidate.
 * They are the only endpoints that fan out across several pillars to build one
 * response — /weekly-report composes the scorecard, cost, delivery and briefing
 * models; /briefing ranks alerts across every pillar; /copilot/digest summarises
 * the lot — and they are what the optimisation commits have repeatedly had to
 * go back and fix.
 *
 * HOW THIS DIFFERS FROM THE REAL WORLD, DELIBERATELY
 * The snapshot cards poll these on a 30s timer, not in a tight loop. This
 * script is NOT a fidelity model of that; it is a saturation probe. Each VU
 * walks the composite set back to back with a short think time, which is far
 * harsher than any tab. Read its output as "the endpoint that falls over first
 * and roughly when", never as "what users experience" — dashboard-poll.js is
 * the script that answers that.
 *
 * WHAT SATURATES FIRST, AND WHY IT IS WORTH WATCHING
 * A single uvicorn worker runs one event loop, and these handlers are
 * synchronous SQLAlchemy: FastAPI runs a `def` (non-async) endpoint in a
 * threadpool, so concurrency is bounded by that threadpool and then by the
 * connection pool underneath it — 5 connections plus 10 overflow
 * (backend/database.py takes SQLAlchemy's defaults). Past roughly 15 genuinely
 * concurrent database-touching requests, further arrivals queue on
 * pool checkout rather than on CPU, and latency goes to a cliff rather than
 * degrading smoothly. Watch for the knee, not the average.
 *
 *   *** Defaults to localhost. See load/config.js for the guard. ***
 *
 * Run:
 *   LOAD_USER=admin LOAD_PASS=... VUS=10 DURATION=2m k6 run load/read-models.js
 *   ... SET=all ...          composites plus every pillar read-model
 *   ... PER_ENDPOINT=1 ...   print a per-endpoint p95 row
 *   ... THINK=0 ...          no think time at all — pure saturation
 */
import http from "k6/http";
import { check, sleep } from "k6";
import { Trend } from "k6/metrics";

import { READ_MODELS, READ_MODELS_COMPOSITE } from "./endpoints.js";
import { readModelThresholds } from "./thresholds.js";
import {
  announce,
  assertTargetIsSafe,
  envInt,
  envStr,
  login,
  req,
} from "./config.js";

const VUS = envInt("VUS", 10);
const DURATION = envStr("DURATION", "1m");
const RAMP = envStr("RAMP", "30s");
const PER_ENDPOINT = __ENV.PER_ENDPOINT === "1";

// THINK may legitimately be 0, so envInt's "must be >= 1" rule does not apply.
const THINK_SECONDS = __ENV.THINK === undefined || __ENV.THINK === ""
  ? 1
  : Math.max(0, parseFloat(__ENV.THINK) || 0);

// Composites only by default. They are the heavy ones, and mixing in 32 cheap
// pillar endpoints would dilute the p95 that matters into an average that hides
// the problem.
const SET = envStr("SET", "composite");
const PATHS = SET === "all" ? READ_MODELS : READ_MODELS_COMPOSITE;

const sweepDuration = new Trend("read_model_sweep_duration", true);

export const options = {
  scenarios: {
    ramp: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        // Ramp in rather than starting flat: a cliff shows up as the point on
        // the ramp where latency departs, and a flat start hides which VU count
        // caused it.
        { duration: RAMP, target: VUS },
        { duration: DURATION, target: VUS },
        { duration: RAMP, target: 0 },
      ],
      gracefulRampDown: "10s",
    },
  },
  thresholds: readModelThresholds(PATHS, PER_ENDPOINT),
};

export function setup() {
  assertTargetIsSafe();
  announce("read-models", {
    vus: VUS,
    ramp: RAMP,
    duration: DURATION,
    set: SET,
    endpoints: PATHS.length,
    think_s: THINK_SECONDS,
  });
  return login();
}

export default function (auth) {
  const started = Date.now();

  // Sequential, not batched. Each VU stands for one caller waiting on one
  // answer at a time; batching would conflate "the server is slow" with "the
  // client asked for six things at once", and the point here is to find the
  // concurrency at which a single request goes bad.
  for (const path of PATHS) {
    const request = req(path, auth.token);
    const res = http.get(request.url, request.params);
    check(res, {
      "read-model 2xx": (r) => r.status >= 200 && r.status < 300,
    });
  }

  sweepDuration.add(Date.now() - started);
  if (THINK_SECONDS > 0) sleep(THINK_SECONDS);
}
