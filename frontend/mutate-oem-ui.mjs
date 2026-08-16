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
  "components/AddConnectedEquipment.test.tsx " +
  "components/OemMachineRegistry.test.tsx " +
  "app/oem/page.test.tsx app/login/page.test.tsx " +
  '"app/claim/[code]/page.test.tsx"';

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
    label: "the warranty dates are dropped on the way to the endpoint",
    file: "components/OemMachineRegistry.tsx",
    from: "        warranty_start: warrantyStart || null,\n"
      + "        warranty_end: warrantyEnd || null,\n",
    to: "",
  },
  {
    label: "a blank warranty is sent as \"\", which the endpoint rejects",
    file: "components/OemMachineRegistry.tsx",
    from: "        warranty_start: warrantyStart || null,",
    to: "        warranty_start: warrantyStart,",
  },
  {
    label: "the cover is cleared between machines off one delivery note",
    file: "components/OemMachineRegistry.tsx",
    from: '      setSerial("");',
    to: '      setSerial("");\n      setWarrantyStart("");\n      setWarrantyEnd("");',
  },
  {
    label: "an unshared commissioning check renders as a FAILURE",
    file: "app/oem/page.tsx",
    from: '                          {c.passed === null ? "–" : c.passed ? "✓" : "✗"}',
    to: '                          {c.passed ? "✓" : "✗"}',
  },
  {
    label: "commissioning reads 'incomplete' when a check is merely unshared",
    file: "app/oem/page.tsx",
    from: "                      {detail.commissioning.ready === null\n"
      + '                        ? "· not fully shared"\n'
      + "                        : detail.commissioning.ready\n"
      + '                          ? "· complete"\n'
      + '                          : "· incomplete"}',
    to: '                      {detail.commissioning.ready ? "· complete" : "· incomplete"}',
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
    // The `setFailed` tail disambiguates this from the byte-identical parse in
    // the link handler. Without it the pattern matched twice and the harness
    // SKIPped — a mutation that tests nothing while still reading as one.
    label: "a refused change is swallowed into a generic message",
    file: "components/ConnectedEquipment.tsx",
    from: "          if (parsed?.detail) detail = String(parsed.detail);\n" +
      "        } catch {\n" +
      "          /* a non-JSON error body is still an error; keep the text */\n" +
      "        }\n" +
      "        setFailed",
    to: "          if (parsed?.detail) detail = raw;\n" +
      "        } catch {\n" +
      "          /* a non-JSON error body is still an error; keep the text */\n" +
      "        }\n" +
      "        setFailed",
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

  // --- claiming a machine (ADR-0019) --------------------------------------
  {
    label: "LOOKING UP a code claims the machine (a URL becomes consent)",
    file: "components/AddConnectedEquipment.tsx",
    from: "      const p = await apiGet<Preview>(\n" +
      "        `/connected-equipment/claim/${encodeURIComponent(code.trim())}`,\n" +
      "      );",
    to: "      const p = await apiPost<Preview>(\n" +
      "        `/connected-equipment/claim/${encodeURIComponent(code.trim())}`,\n" +
      "        { grants: [] },\n" +
      "      );",
  },
  {
    label: "the confirmation sends grants nobody ticked",
    file: "components/AddConnectedEquipment.tsx",
    from: "        { grants: chosen },",
    to: "        { grants: preview.available_grants.map((g) => g.key) },",
  },
  {
    label: "an existing agreement is shown as unticked (reads as turning it off)",
    file: "components/AddConnectedEquipment.tsx",
    from: "      setChosen(p.already_granted || []);",
    to: "      setChosen([]);",
  },
  {
    label: "a refused lookup is swallowed into a generic message",
    file: "components/AddConnectedEquipment.tsx",
    from: "      setError(detailOf(err));\n    } finally {\n      setBusy(false);\n    }\n  }\n\n  async function confirm()",
    to: "      setError('Something went wrong.');\n    } finally {\n      setBusy(false);\n    }\n  }\n\n  async function confirm()",
  },
  // --- saying which machine on the floor a serial is (ADR-0019) ------------
  {
    label: "unlinking sends 0 — a machine id nobody has — instead of null",
    file: "components/ConnectedEquipment.tsx",
    from: '                          ev.target.value === "" ? null : Number(ev.target.value),',
    to: "                          Number(ev.target.value),",
  },
  {
    label: "a refused link is swallowed into a generic message",
    file: "components/ConnectedEquipment.tsx",
    from: "        setLinkFailed((prev) => ({ ...prev, [installationId]: detail }));",
    to: '        setLinkFailed((prev) => ({ ...prev, [installationId]: "That did not work." }));',
  },
  {
    label: "the 409 is dumped as its raw JSON envelope",
    file: "components/ConnectedEquipment.tsx",
    from: "          if (parsed?.detail) detail = String(parsed.detail);\n" +
      "        } catch {\n" +
      "          /* a non-JSON error body is still an error; keep the text */\n" +
      "        }\n" +
      "        setLinkFailed",
    to: "          if (parsed?.detail) detail = raw;\n" +
      "        } catch {\n" +
      "          /* a non-JSON error body is still an error; keep the text */\n" +
      "        }\n" +
      "        setLinkFailed",
  },
  {
    label: "a non-Admin gets a working machine selector",
    file: "components/ConnectedEquipment.tsx",
    from: "                      disabled={!isAdmin || busy(`link:${e.installation_id}`)}",
    to: "                      disabled={busy(`link:${e.installation_id}`)}",
  },
  {
    label: "the missing-link prompt disappears, so nobody knows why it stalls",
    file: "components/ConnectedEquipment.tsx",
    from: "                        needed before its maker can commission it",
    to: "                        &nbsp;",
  },
  // --- the QR deep link ----------------------------------------------------
  {
    label: "a scanned code is dropped, so the label is retyped by hand",
    file: "components/AddConnectedEquipment.tsx",
    from: "  const [code, setCode] = useState(initialCode);",
    to: '  const [code, setCode] = useState("");',
  },
  {
    label: "the panel stays shut, so a scan lands on a page with no form",
    file: "components/AddConnectedEquipment.tsx",
    from: "  const [open, setOpen] = useState(Boolean(initialCode));",
    to: "  const [open, setOpen] = useState(false);",
  },
  {
    label: "a percent-encoded code is passed through undecoded",
    file: "app/claim/[code]/page.tsx",
    from: "  const code = decodeURIComponent(\n" +
      "    Array.isArray(params?.code) ? params.code[0] : params?.code || \"\",\n" +
      "  );",
    to: '  const code = Array.isArray(params?.code) ? params.code[0] : params?.code || "";',
  },
  {
    label: "a MANUFACTURER is offered the factory's claim form",
    file: "app/claim/[code]/page.tsx",
    from: "  const oem = signedIn && isOemSession();",
    to: "  const oem = false;",
  },
  {
    label: "signing in forgets the code that was scanned",
    file: "app/claim/[code]/page.tsx",
    from: '      localStorage.setItem("afterLogin", `/claim/${encodeURIComponent(code)}`);',
    to: "      void code;",
  },
  {
    label: "the post-login return accepts ANY destination (an open redirect)",
    file: "app/login/page.tsx",
    from: '      if (back && back.startsWith("/") && !back.startsWith("//")) {',
    to: "      if (back) {",
  },

  {
    label: "a refused confirmation reports success anyway",
    file: "components/AddConnectedEquipment.tsx",
    from: "    } catch (err) {\n      setError(detailOf(err));\n    } finally {\n      setBusy(false);\n    }\n  }\n\n  return (",
    to: "    } catch (err) {\n      onAdded();\n    } finally {\n      setBusy(false);\n    }\n  }\n\n  return (",
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

// LINE ENDINGS ARE NOT COSMETIC HERE.
//
// Every multi-line pattern below is written with LF. Git materialises these
// files with CRLF on Windows, so after a checkout the patterns match NOTHING
// and six mutations reported "SKIP (pattern hits 0x)" -- which reads almost
// exactly like a pass in a wall of output. The harness had silently stopped
// testing the claim flow, and only because of how git last wrote the file.
//
// So: match against a normalised copy, and put the file back the way it was
// found, CRLF and all, so this never shows up as a spurious diff either.
const CRLF = /\r\n/g;
const originals = new Map();   // file -> normalised (LF) source, for matching
const wasCrlf = new Map();     // file -> did it arrive with CRLF
for (const m of MUTATIONS) {
  if (originals.has(m.file)) continue;
  const raw = readFileSync(m.file, "utf8");
  // `.includes`, not `CRLF.test`: a /g regex carries lastIndex between calls,
  // so `.test` would answer true, false, true, … down the file list.
  wasCrlf.set(m.file, raw.includes("\r\n"));
  originals.set(m.file, raw.replace(CRLF, "\n"));
}

/** Write `text` back in the file's own line-ending style. */
function put(file, text) {
  writeFileSync(file, wasCrlf.get(file) ? text.replace(/\n/g, "\r\n") : text);
}

/** Read a file back as normalised LF, for the restored-cleanly check. */
function readNormalised(file) {
  return readFileSync(file, "utf8").replace(CRLF, "\n");
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
    console.log(m.label.padEnd(62) + `SKIP - NOT TESTED (pattern hits ${hits}x in ${m.file})`);
    survived.push(`${m.label} (pattern did not apply)`);
    continue;
  }
  put(m.file, source.replace(m.from, m.to));
  let caught;
  try {
    caught = !suitesPass();
  } finally {
    put(m.file, source);
  }
  console.log(m.label.padEnd(62) + (caught ? "caught" : "SURVIVED"));
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
