/**
 * Mutation harness for the OEM user interface (ADR-0017).
 *
 * The backend harnesses (mutate_oem_auth, mutate_oem_sharing, mutate_oem_service)
 * prove the server refuses what it should. None of them can prove the LAST step,
 * which is the one a service engineer actually reads: that a withheld field is
 * rendered as "not shared" rather than as a zero, and that a machine whose owner
 * declined to share health is not counted as one that has gone offline.
 *
 * Both of those are one careless `?? 0` away, neither changes a single server
 * response, and both send somebody to a site where nothing is wrong.
 *
 * Run: node mutate-oem-ui.mjs        (from frontend/)
 */
import { execSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";

const SUITES =
  "lib/oem.test.ts components/ConnectedEquipment.test.tsx " +
  "app/oem/page.test.tsx app/login/page.test.tsx";

const MUTATIONS = [
  // --- a privacy setting must never render as an operational fact ----------
  {
    label: "a withheld value renders as its zero instead of 'not shared'",
    file: "lib/oem.ts",
    from: '  if (!granted) return "not shared";',
    to: '  if (!granted && value === null) return "not shared";',
  },
  {
    label: "'no data' and 'not shared' collapse into one answer",
    file: "lib/oem.ts",
    from: '  if (value === null || value === undefined) return "no data";',
    to: '  if (value === null || value === undefined) return "not shared";',
  },
  {
    label: "an unshared machine is counted as OFFLINE",
    file: "lib/oem.ts",
    from: "    if (stale === null) unknown += 1;",
    to: "    if (stale === null) offline += 1;",
  },
  {
    label: "an unrecorded warranty is counted as cover",
    file: "lib/oem.ts",
    from: "      (m) => m.warranty_end && new Date(m.warranty_end).getTime() > now,",
    to: "      (m) => !m.warranty_end || new Date(m.warranty_end).getTime() > now,",
  },

  // --- the portal is chosen by the PRINCIPAL, not by a role string ---------
  {
    label: "an OEM session is recognised by its role name",
    file: "lib/oem.ts",
    from: '    return JSON.parse(atob(token.split(".")[1])).principal === "oem";',
    to: '    return JSON.parse(atob(token.split(".")[1])).role === "OEM_ADMIN";',
  },

  // --- the factory's consent panel ----------------------------------------
  {
    label: "the panel stops naming what is NOT shared",
    file: "components/ConnectedEquipment.tsx",
    from: "                : `Not shared: ${withheld.map((g) => g.label).join(\", \")}.`}",
    to: '                : ""}',
  },
  {
    label: "a non-Admin gets working sharing controls",
    file: "components/ConnectedEquipment.tsx",
    from: "                    disabled={!isAdmin}",
    to: "                    disabled={false}",
  },
  {
    label: "a refused change is swallowed into a generic message",
    file: "components/ConnectedEquipment.tsx",
    from: "          if (parsed?.detail) detail = String(parsed.detail);",
    to: "          if (parsed?.detail) detail = raw;",
  },
  {
    label: "a failed load renders as an empty state",
    file: "components/ConnectedEquipment.tsx",
    from: "      <LoadError message={error} />",
    to: "      {null}",
  },

  // --- a manufacturer and a factory user belong on different screens -------
  {
    label: "everybody is sent to the factory dashboard after signing in",
    file: "app/login/page.tsx",
    from: '      router.push(isOemSession() ? "/oem" : "/dashboard");',
    to: '      router.push("/dashboard");',
  },
  {
    label: "any failure is read as 'you are in the wrong portal'",
    file: "app/oem/page.tsx",
    from: "      const denied = e instanceof OemRequestError && e.status === 401;",
    to: "      const denied = true;",
  },
  {
    label: "a wrong-portal 401 is shown as an error banner instead",
    file: "app/oem/page.tsx",
    from: "      const denied = e instanceof OemRequestError && e.status === 401;",
    to: "      const denied = false;",
  },
];

function suitesPass() {
  try {
    execSync(`npx vitest run ${SUITES}`, { stdio: "pipe" });
    return true;
  } catch {
    return false;
  }
}

const originals = new Map();
for (const m of MUTATIONS) {
  if (!originals.has(m.file)) originals.set(m.file, readFileSync(m.file, "utf8"));
}

if (!suitesPass()) {
  console.log("ABORT: the suites are already failing before any mutation");
  process.exit(2);
}
console.log("baseline: green\n");
console.log("mutation".padEnd(62) + "verdict");
console.log("-".repeat(80));

const survived = [];
for (const m of MUTATIONS) {
  const source = originals.get(m.file);
  const hits = source.split(m.from).length - 1;
  if (hits !== 1) {
    console.log(m.label.padEnd(62) + `SKIP (pattern hits ${hits}x in ${m.file})`);
    survived.push(`${m.label} (pattern did not apply)`);
    continue;
  }
  writeFileSync(m.file, source.replace(m.from, m.to));
  let caught;
  try {
    caught = !suitesPass();
  } finally {
    writeFileSync(m.file, source);
  }
  console.log(m.label.padEnd(62) + (caught ? "caught" : "SURVIVED"));
  if (!caught) survived.push(m.label);
}

const dirty = [...originals].filter(([f, o]) => readFileSync(f, "utf8") !== o);
console.log(`\nsource files restored: ${dirty.length === 0 ? "yes" : "NO - DIRTY"}`);
if (dirty.length) process.exit(3);
if (survived.length) {
  console.log(`${survived.length} MUTATION(S) SURVIVED - investigate each:`);
  for (const s of survived) console.log("   *", s);
  process.exit(1);
}
console.log(`all ${MUTATIONS.length} mutations caught`);
