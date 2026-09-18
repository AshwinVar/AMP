/**
 * Mutation harness for the service-contract user interface (ADR-0021).
 *
 * The backend harnesses (mutate_contract_engine, mutate_service_contracts,
 * mutate_contract_integration) prove the server computes, refuses and withholds
 * what it should. None of them can prove the LAST inch a person reads: that
 * "No data" is not drawn as uptime, that a credit is the paisa the server sent,
 * that consent is a box somebody ticked, that a withheld statement shows no
 * numbers, and that Accept names the exact revision on screen. Each mutation
 * below is one plausible edit that breaks one of those without changing a
 * single server response. Every one must turn a suite red.
 *
 * Run: node mutate-contracts-ui.mjs        (from frontend/)
 */
import { execSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";

const SUITES = [
  "lib/contracts.test.ts", "lib/contractClients.test.ts", "lib/money.test.ts",
  "lib/modules.test.ts", "lib/oem.test.ts",
  "components/contracts/StatementView.test.tsx", "components/contracts/AcceptancePanel.test.tsx",
  "components/contracts/DisputePanel.test.tsx", "components/contracts/wiring.test.ts",
  "components/ServiceContracts.test.tsx", "components/OemContracts.test.tsx",
  "app/oem/page.test.tsx",
].join(" ");

const MUTATIONS = [
  // --- no data is never uptime or downtime -----------------------------------
  {
    label: "UNMEASURED is labelled as uptime",
    file: "lib/contracts.ts",
    from: '  UNMEASURED: "No data",',
    to: '  UNMEASURED: "Available",',
  },
  {
    label: "a tile silently drops the unmeasured seconds",
    file: "lib/contracts.ts",
    from: "    const seconds = totals[TOTAL_KEY[bucket]];",
    to: '    const seconds = bucket === "UNMEASURED" ? 0 : totals[TOTAL_KEY[bucket]];',
  },
  {
    label: "the buckets are never checked against the covered time",
    file: "lib/contracts.ts",
    from: "  return BUCKETS.reduce((sum, b) => sum + totals[TOTAL_KEY[b]], 0) === totals.covered_seconds;",
    to: "  return true;",
  },
  {
    label: "durations are rounded to minutes",
    file: "lib/contracts.ts",
    from: "  if (s > 0) parts.push(`${h > 0 || m > 0 ? pad2(s) : s} s`);",
    to: "",
  },
  {
    label: "a fractional duration is shown as if it were exact",
    file: "lib/contracts.ts",
    from: '  if (!Number.isSafeInteger(seconds) || seconds < 0) return "—";',
    to: '  if (seconds < 0) return "—";',
  },

  // --- money --------------------------------------------------------------------
  {
    label: "contract money goes through a float",
    file: "lib/money.ts",
    from: '  return symbol + grouped + "." + match[2];',
    to: '  return symbol + new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2 }).format(Number(amount));',
  },
  {
    label: "malformed money is displayed instead of refused",
    file: "lib/money.ts",
    from: "  if (!match) {\n    throw new RangeError",
    to: "  if (!match) {\n    return amount;\n    throw new RangeError",
  },
  {
    label: "a missing credit renders as zero",
    file: "components/contracts/StatementView.tsx",
    from: "  } else if (credit.amount === null) {\n    line = <>No credit is computed for this period.</>;",
    to: "  } else if (credit.amount === null) {\n    line = <>Credit: {money(\"0.00\")}</>;",
  },
  {
    label: "a pending-disputes period shows an amount instead of a range",
    file: "components/contracts/StatementView.tsx",
    from: '  if (state === "pending_disputes") {',
    to: '  if (state === "pending_disputes" && credit.amount !== null) {',
  },

  // --- the consistency check is over the exact bytes -----------------------------
  {
    label: "the browser hashes a re-serialisation, not the downloaded bytes",
    file: "lib/contracts.ts",
    from: "  const data = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);",
    to: "  const raw = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);\n"
      + "  const data = new TextEncoder().encode(JSON.stringify(JSON.parse(new TextDecoder().decode(raw))));",
  },
  {
    label: "a hash prefix counts as a match",
    file: "lib/contracts.ts",
    from: "  return { hash, matches: hash === expectedHash };",
    to: "  return { hash, matches: hash.startsWith(expectedHash) };",
  },
  {
    label: "the panel reports a local match whatever the bytes hash to",
    file: "components/contracts/AcceptancePanel.tsx",
    from: "            {check.local.matches ? \", which matches revision \" + statement.revision",
    to: "            {true ? \", which matches revision \" + statement.revision",
  },

  // --- acceptance is bound to the revision ------------------------------------------
  {
    label: "Accept sends no revision (the hash alone)",
    file: "components/contracts/AcceptancePanel.tsx",
    from: "        await api.acceptStatement(contractId, statement.id, statement.content_hash,\n"
      + "                                  statement.revision);",
    to: "        await api.acceptStatement(contractId, statement.id, statement.content_hash, 1);",
  },
  {
    label: "a cancelled acceptance counts as this side's acceptance",
    file: "lib/contracts.ts",
    from: "  return acceptances.find((a) => a.party === side && a.valid) ?? null;",
    to: "  return acceptances.find((a) => a.party === side) ?? null;",
  },
  {
    label: "a cancelled acceptance is shown as valid",
    file: "components/contracts/AcceptancePanel.tsx",
    from: "                  {a.valid ? (",
    to: "                  {true ? (",
  },
  {
    label: "open disputes do not block Accept",
    file: "lib/contracts.ts",
    from: "  const live = liveDisputes(statement.disputes).length;",
    to: "  const live = 0;",
  },
  {
    label: "rule-DISPUTED time does not block Accept",
    file: "lib/contracts.ts",
    from: '  if (statement.content.sla.state === "pending_disputes" && live === 0) {',
    to: "  if (false) {",
  },
  {
    label: "an agreed statement can be accepted again",
    file: "lib/contracts.ts",
    from: '  if (statement.agreed) return ["Both parties accepted this revision; it is final"];',
    to: "",
  },

  // --- consent -----------------------------------------------------------------------
  {
    label: "Accept contract is enabled before consent is ticked",
    file: "components/ServiceContracts.tsx",
    from: "          <button type=\"button\" disabled={!consent || !v1 || busy(\"accept\")}",
    to: "          <button type=\"button\" disabled={!v1 || busy(\"accept\")}",
  },
  {
    label: "the grant is always sent as true",
    file: "components/ServiceContracts.tsx",
    from: "                    serviceContractsApi.accept(detail.id, v1!.terms_hash, consent))}",
    to: "                    serviceContractsApi.accept(detail.id, v1!.terms_hash, true))}",
  },
  {
    label: "the transport drops grant_downtime_sharing",
    file: "lib/serviceContracts.ts",
    from: "                            { terms_hash: termsHash, grant_downtime_sharing: grantDowntimeSharing }),",
    to: "                            { terms_hash: termsHash }),",
  },
  {
    label: "the disclosure no longer says the grant covers the whole relationship",
    file: "components/ServiceContracts.tsx",
    from: "          This grant (SHARE_DOWNTIME) covers your whole relationship with {detail.oem_code}, not\n"
      + "          only these machines.",
    to: "          This grant (SHARE_DOWNTIME) covers these machines.",
  },
  {
    label: "a Supervisor is offered the decision",
    file: "components/ServiceContracts.tsx",
    from: '  const canSign = getUserRole() === "Admin";',
    to: '  const canSign = getUserRole() !== "Operator";',
  },
  {
    label: "unmapped reasons are presented as attributed",
    file: "components/ServiceContracts.tsx",
    from: '                        : "not in the terms: that downtime would be Disputed"}',
    to: '                        : "attributed"}',
  },

  // --- withheld is a consent state, with no numbers ------------------------------------
  {
    label: "a withheld statement is shown as an error",
    file: "components/contracts/ContractWorkspace.tsx",
    from: '          setOpened(err.withheld ? { kind: "withheld", message: "" }',
    to: '          setOpened(false ? { kind: "withheld", message: "" }',
  },
  {
    label: "compute is offered to a manufacturer the factory withheld from",
    file: "components/contracts/ContractWorkspace.tsx",
    from: "                        {canManage && !withheld && closedPeriod(p, bundle.loadedAt) && p.terms_version !== null",
    to: "                        {canManage && closedPeriod(p, bundle.loadedAt) && p.terms_version !== null",
  },
  {
    label: "the refusal's withheld flag is ignored",
    file: "lib/contracts.ts",
    from: "    && (detail as Rec).withheld === true);",
    to: "    && false);",
  },
  {
    label: "withheld history details are printed anyway",
    file: "components/contracts/ContractHistory.tsx",
    from: '                {r.details_withheld ? "withheld: the factory has withdrawn downtime sharing" : r.details}',
    to: "                {r.details}",
  },

  // --- capabilities and wiring ----------------------------------------------------------
  {
    label: "a viewer is offered compute and accept",
    file: "components/OemContracts.tsx",
    from: '  const canManage = capabilities.includes("manage_contracts");\n'
      + '  const canSign = capabilities.includes("sign_contracts");',
    to: "  const canManage = true;\n  const canSign = true;",
  },
  {
    label: "contracts render for a role without read_contracts",
    file: "components/OemContracts.tsx",
    from: '  if (!capabilities.includes("read_contracts")) return null;',
    to: "",
  },
  {
    label: "propose sends a hash other than version 1's",
    file: "components/OemContracts.tsx",
    from: "                onClick={() => act(\"propose\", () => oemContractsApi.propose(detail.id, v1.terms_hash))}",
    to: "                onClick={() => act(\"propose\", () => oemContractsApi.propose(detail.id, \"\"))}",
  },
  {
    label: "the dashboard renders contracts outside the role and plan gate",
    file: "app/dashboard/page.tsx",
    from: '      {renderSection("contracts", (\n        <ServiceContracts />\n      ))}',
    to: '      {activeView === "contracts" && <ServiceContracts />}',
  },
  {
    label: "the portal passes every capability instead of the principal's",
    file: "app/oem/page.tsx",
    from: "          <OemContracts capabilities={identity.capabilities} fleet={machines} />",
    to: '          <OemContracts capabilities={["read_contracts", "manage_contracts", "sign_contracts"]} fleet={machines} />',
  },
  {
    label: "Service Contracts moves out of the core pack",
    file: "lib/modules.ts",
    from: '  { key: "contracts",      label: "Service Contracts",  icon: "§", module: "core" },',
    to: '  { key: "contracts",      label: "Service Contracts",  icon: "§", module: "operations" },',
  },
  {
    label: "a dispute can be settled as Disputed",
    file: "lib/contracts.ts",
    from: 'export const RESOLUTION_BUCKETS: readonly Bucket[] = ["AVAILABLE", "OEM", "FACTORY", "UNMEASURED"];',
    to: "export const RESOLUTION_BUCKETS: readonly Bucket[] = BUCKETS;",
  },
  {
    label: "the raising party can accept its own resolution",
    file: "components/contracts/DisputePanel.tsx",
    from: "                    && d.resolution_proposed_by_party !== api.side && d.resolution_bucket && (",
    to: "                    && d.resolution_bucket && (",
  },
  {
    label: "a typed window is sent without UTC normalisation",
    file: "components/contracts/DisputePanel.tsx",
    from: "        installation_id: Number(installation), window_start: windowStart, window_end: windowEnd,",
    to: "        installation_id: Number(installation), window_start: start, window_end: end,",
  },
  {
    label: "the trust note about factory-provisioned statuses is dropped",
    file: "components/contracts/ContractTerms.tsx",
    from: "            Statuses from these sources are provisioned or posted by the factory. They are not\n"
      + "            authenticated as coming from the manufacturer.",
    to: "            Statuses from these sources come from the machine.",
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

// Patterns are written with LF; files may be CRLF on Windows. Match against a
// normalised copy and write back in the file's own style (see mutate-oem-ui.mjs).
const CRLF = /\r\n/g;
const originals = new Map();
const wasCrlf = new Map();
for (const m of MUTATIONS) {
  if (originals.has(m.file)) continue;
  const raw = readFileSync(m.file, "utf8");
  wasCrlf.set(m.file, raw.includes("\r\n"));
  originals.set(m.file, raw.replace(CRLF, "\n"));
}

function put(file, text) {
  writeFileSync(file, wasCrlf.get(file) ? text.replace(/\n/g, "\r\n") : text);
}

function readNormalised(file) {
  return readFileSync(file, "utf8").replace(CRLF, "\n");
}

const unmatched = MUTATIONS.filter((m) => originals.get(m.file).split(m.from).length - 1 !== 1);
if (unmatched.length) {
  for (const m of unmatched) {
    console.log(`PATTERN PROBLEM: ${m.label} (hits ${originals.get(m.file).split(m.from).length - 1}x in ${m.file})`);
  }
  process.exit(2);
}
if (process.argv.includes("--check")) {
  console.log(`all ${MUTATIONS.length} mutation patterns apply exactly once`);
  process.exit(0);
}

if (!suitesPass()) {
  console.log("ABORT: the suites are already failing before any mutation");
  process.exit(2);
}
console.log("baseline: green\n");
console.log("mutation".padEnd(76) + "verdict");
console.log("-".repeat(90));

const survived = [];
for (const m of MUTATIONS) {
  const source = originals.get(m.file);
  put(m.file, source.replace(m.from, m.to));
  let caught;
  try {
    caught = !suitesPass();
  } finally {
    put(m.file, source);
  }
  console.log(m.label.padEnd(76) + (caught ? "caught" : "SURVIVED"));
  if (!caught) survived.push(m.label);
}

const dirty = [...originals].filter(([f, o]) => readNormalised(f) !== o);
console.log(`\nsource files restored: ${dirty.length === 0 ? "yes" : "NO - DIRTY"}`);
if (dirty.length) process.exit(3);
if (survived.length) {
  console.log(`${survived.length} MUTATION(S) SURVIVED - investigate each:`);
  for (const s of survived) console.log("   *", s);
  process.exit(1);
}
console.log(`all ${MUTATIONS.length} mutations caught`);
