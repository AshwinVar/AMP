/**
 * The load profile that actually exists in production, and the one nobody has
 * ever measured: N open dashboard tabs, each issuing the real fetchAll set
 * every three seconds.
 *
 * WHY THIS SCRIPT EXISTS
 * Every performance conversation about AMP so far has been about a single
 * endpoint in isolation — "265ms to 13ms" on /analytics/executive-oee, "714ms
 * to 28ms" on the flow read-model. None of that describes what the server is
 * actually asked to do. What it is asked to do is this: for every dashboard tab
 * left open on a plant floor, 46 requests arrive every 3 seconds, forever,
 * against ONE uvicorn worker (backend/Procfile has no --workers) drawing on a
 * default SQLAlchemy pool of 5 + 10 overflow (backend/database.py) with no
 * cache anywhere in front of it. A supervisor's tab, an office tab and a
 * wall-mounted display is three tabs and roughly 46 requests per second before
 * anyone clicks anything.
 *
 * WHAT IS MODELLED, AND WHY IT IS MODELLED THAT WAY
 *   * One VU = one open tab. Not one user "doing a journey" — the dashboard
 *     polls whether or not anyone touches it.
 *   * batchPerHost: 6 mirrors the browser's per-origin connection cap. Firing
 *     all 46 at once would be a load no real tab can produce, and would make the
 *     server look worse than it is. Six at a time is the honest shape.
 *   * The round is timed end to end and, if it overruns 3s, the script does NOT
 *     sleep — because usePolling (frontend/lib/usePolling.ts) skips the next
 *     tick while a round is still in flight and starts the following one
 *     immediately. Modelling the sleep unconditionally would invent recovery
 *     time the real client does not take.
 *
 * THE METRIC TO READ FIRST
 * `dashboard_round_duration`. Its p95 crossing 3000ms is the moment the product
 * quietly breaks: usePolling starts skipping ticks, the tab stops refreshing at
 * 3s, and the operator reads stale numbers with nothing on screen saying so.
 * Per-request p95 is a diagnostic; the round is the user-visible truth.
 *
 *   *** Defaults to localhost. See load/config.js for the guard. ***
 *
 * Run:
 *   LOAD_USER=admin LOAD_PASS=... VUS=3 DURATION=2m k6 run load/dashboard-poll.js
 *   ... WITH_CARDS=1 ...     also layer the 30s snapshot-card read-model polls
 *   ... PER_ENDPOINT=1 ...   print a per-endpoint p95 row (46 extra rows)
 */
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

import {
  DASHBOARD_BLOCKING,
  DASHBOARD_INTERVAL_MS,
  DASHBOARD_OPTIONAL,
  DASHBOARD_ROUND,
  READ_MODELS,
} from "./endpoints.js";
import { dashboardThresholds } from "./thresholds.js";
import { announce, assertTargetIsSafe, envInt, envStr, login, req } from "./config.js";

const VUS = envInt("VUS", 3);
const DURATION = envStr("DURATION", "1m");
const WITH_CARDS = __ENV.WITH_CARDS === "1";
const PER_ENDPOINT = __ENV.PER_ENDPOINT === "1";

// The snapshot cards each refresh their own read-model on a timer of their own
// (grep setInterval in frontend/components: 32 at 30000, the weekly report at
// 60000, the connectivity panel at 8000). 30s is taken as the representative
// cadence. Layering them in is more faithful — a real tab does both — but it is
// opt-in, because the fetchAll round is the thing under test and mixing the two
// makes a first baseline harder to read. Note also that only the MOUNTED cards
// poll on a real tab, so this layer is an upper bound, not an average.
const CARD_INTERVAL_MS = 30000;

export const options = {
  scenarios: {
    open_tabs: {
      executor: "constant-vus",
      vus: VUS,
      duration: DURATION,
      gracefulStop: "10s",
    },
  },
  // Both set explicitly, and both to 6, to mirror a browser's per-origin
  // connection limit. k6's defaults (batch 20 / batchPerHost 6) would let the
  // blocking three and the optional 43 overlap differently to a real tab.
  batch: 6,
  batchPerHost: 6,
  // Connection reuse left ON: browsers keep-alive, and turning it off would
  // measure TLS handshakes the real client does not pay for on every request.
  thresholds: dashboardThresholds(PER_ENDPOINT),
};

const roundDuration = new Trend("dashboard_round_duration", true);
const blockingDuration = new Trend("dashboard_blocking_duration", true);
const roundOverran = new Rate("dashboard_round_overran");
const roundRequests = new Counter("dashboard_round_requests");
const optionalNon2xx = new Rate("dashboard_optional_non_2xx");

// Per-VU state: each k6 VU gets its own module instance, so this is genuinely
// "this tab's last card refresh" rather than a shared clock.
let lastCardPoll = 0;

export function setup() {
  assertTargetIsSafe();
  announce("dashboard-poll", {
    vus: VUS,
    duration: DURATION,
    round_size: DASHBOARD_ROUND.length,
    interval_ms: DASHBOARD_INTERVAL_MS,
    with_cards: WITH_CARDS,
  });
  return login();
}

export default function (auth) {
  const started = Date.now();

  // Phase 1: the Promise.all the dashboard awaits before rendering anything.
  // Its latency is first paint, so it is tagged and budgeted separately.
  const blocking = http.batch(
    DASHBOARD_BLOCKING.map((path) => req(path, auth.token, { phase: "blocking" })),
  );
  blockingDuration.add(Date.now() - started);
  for (let i = 0; i < blocking.length; i++) {
    check(blocking[i], {
      "blocking endpoint 2xx": (r) => r.status >= 200 && r.status < 300,
    });
  }

  // Phase 2: the Promise.allSettled. A 4xx here degrades one card rather than
  // the page, which is why the check is "not 5xx" — the dashboard genuinely
  // tolerates a plan-gated 403, but a 500 under load is the failure we are
  // hunting. Non-2xx is still counted, separately, so it cannot hide.
  const optional = http.batch(
    DASHBOARD_OPTIONAL.map((path) => req(path, auth.token, { phase: "optional" })),
  );
  for (let i = 0; i < optional.length; i++) {
    const res = optional[i];
    check(res, {
      "optional endpoint did not 5xx": (r) => r.status < 500 && r.status !== 0,
    });
    optionalNon2xx.add(res.status < 200 || res.status >= 300);
  }

  roundRequests.add(DASHBOARD_ROUND.length);

  if (WITH_CARDS && Date.now() - lastCardPoll >= CARD_INTERVAL_MS) {
    lastCardPoll = Date.now();
    // Deliberately inside the round's timing: on a real tab these share the
    // same six connections as the fetchAll round and delay it, which is exactly
    // the interaction worth measuring.
    const cards = http.batch(
      READ_MODELS.map((path) => req(path, auth.token, { phase: "card" })),
    );
    roundRequests.add(READ_MODELS.length);
    for (let i = 0; i < cards.length; i++) {
      check(cards[i], {
        "card read-model did not 5xx": (r) => r.status < 500 && r.status !== 0,
      });
    }
  }

  const elapsed = Date.now() - started;
  roundDuration.add(elapsed);

  const overran = elapsed > DASHBOARD_INTERVAL_MS;
  roundOverran.add(overran);
  if (overran) {
    // No sleep. usePolling's in-flight guard means the tick that would have
    // fired during this round was skipped, and the next round begins as soon as
    // this one lands. Sleeping here would model a client that backs off, and
    // this client does not back off.
    return;
  }
  sleep((DASHBOARD_INTERVAL_MS - elapsed) / 1000);
}
