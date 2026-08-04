/**
 * Fail if load/endpoints.js has drifted from the dashboard it claims to model.
 *
 * WHY THIS EXISTS
 * DASHBOARD_ROUND is a hand transcription of the fetchAll list in
 * frontend/app/dashboard/page.tsx. The moment someone adds a card and does not
 * update it, this harness stops measuring the real dashboard — while still
 * printing confident-looking numbers. A silently wrong benchmark is worse than
 * no benchmark, because people act on it.
 *
 * Plain Node, no k6 and no running server, so it can be a cheap required CI
 * check rather than something that only runs when someone remembers.
 *
 *   node load/check-drift.mjs
 *
 * Exits 0 when the catalogue matches, 1 with a diff when it does not.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { DASHBOARD_ROUND } from "./endpoints.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const PAGE = path.resolve(here, "..", "frontend", "app", "dashboard", "page.tsx");

/**
 * Pull the apiGet paths out of fetchAll's body.
 *
 * Scoped to the function rather than the whole file on purpose: page.tsx also
 * calls apiGet from /search, /modules, /tenant-config and the individual card
 * handlers, and none of those are part of the 3s round. Matching the whole file
 * would quietly inflate the round and make the drift check pass for the wrong
 * reason.
 */
function fetchAllPaths(source) {
  const start = source.indexOf("async function fetchAll()");
  if (start === -1) {
    throw new Error(
      "Could not find `async function fetchAll()` in page.tsx. Either it was " +
        "renamed or the polling model changed — either way this checker needs " +
        "updating before its result means anything.",
    );
  }
  // fetchAll ends at its catch block; everything after that is other handlers.
  const end = source.indexOf("} catch (error) {", start);
  const body = source.slice(start, end === -1 ? source.length : end);
  const matches = body.match(/apiGet<[^>]*>\("([^"]+)"\)/g) || [];
  return matches.map((m) => m.replace(/^apiGet<[^>]*>\("/, "").replace(/"\)$/, ""));
}

const real = fetchAllPaths(fs.readFileSync(PAGE, "utf8"));
const modelled = DASHBOARD_ROUND;

const sameLength = real.length === modelled.length;
const sameOrder = sameLength && real.every((p, i) => p === modelled[i]);

if (sameOrder) {
  console.log(
    `load/endpoints.js matches fetchAll: ${real.length} endpoints, same order.`,
  );
  process.exit(0);
}

console.error("load/endpoints.js has DRIFTED from frontend/app/dashboard/page.tsx");
console.error(`  fetchAll issues     : ${real.length} requests`);
console.error(`  DASHBOARD_ROUND has : ${modelled.length} requests`);

const onlyReal = real.filter((p) => modelled.indexOf(p) === -1);
const onlyModelled = modelled.filter((p) => real.indexOf(p) === -1);
if (onlyReal.length) console.error(`  missing from load/  : ${onlyReal.join(", ")}`);
if (onlyModelled.length) console.error(`  stale in load/      : ${onlyModelled.join(", ")}`);
if (!onlyReal.length && !onlyModelled.length) {
  console.error("  same set, different order — the round is issued in a different sequence");
}
console.error("Update DASHBOARD_OPTIONAL / DASHBOARD_BLOCKING in load/endpoints.js.");
process.exit(1);
