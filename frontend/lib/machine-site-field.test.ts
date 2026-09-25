/**
 * The Add Machine form must send `site`, because nothing else can.
 *
 * A `Machine` is UNIQUE(tenant_code, site, name), and `site` used to be written
 * ONLY by the MQTT handler, from the topic. So every machine a person added
 * carried an empty site, and the first gateway message published under a real
 * site did not match it — it registered a second machine of the same name.
 *
 * The backend now adopts rather than duplicating, so the trap is closed either
 * way. But a plant with TWO sites still has to say which one a machine is at,
 * and this form is the only place a person can. A field that exists in the API
 * and in no form is a field that does not exist.
 *
 * Asserted against the SOURCE rather than a rendered tree, the same way
 * dashboard-role-gates.test.ts does: the risk is somebody deleting the field or
 * dropping it from the request body while the form still renders perfectly.
 */
import { readFileSync } from "fs";
import { join } from "path";
import { describe, expect, it } from "vitest";

const source = readFileSync(join(process.cwd(), "app/dashboard/page.tsx"), "utf8");

/** The body of addMachine(), so a `site` mentioned elsewhere cannot satisfy this. */
function addMachineBody(text: string): string {
  const start = text.indexOf("async function addMachine(");
  expect(start, "addMachine is gone from the dashboard").toBeGreaterThan(-1);
  const end = text.indexOf("\n  }", text.indexOf('apiPost<Machine>("/machines"', start));
  return text.slice(start, end);
}

describe("the Add Machine form carries the machine's site", () => {
  it("POSTs site along with the rest of the identity", () => {
    const body = addMachineBody(source);
    expect(body).toContain('apiPost<Machine>("/machines"');
    // The shorthand property, not merely the word somewhere in the function.
    // No `s` flag: it needs es2018, and a negated character class already
    // spans newlines, so it was never doing anything here.
    expect(body).toMatch(/apiPost<Machine>\("\/machines",\s*\{[^}]*\bsite,/);
  });

  it("has an input bound to the same state the request sends", () => {
    expect(source).toContain("const [site, setSite] = useState");
    expect(source).toMatch(/value=\{site\}[\s\S]{0,120}onChange=\{\(e\) => setSite\(e\.target\.value\)\}/);
  });

  it("does not make the site required — a single-site plant has nothing to put there", () => {
    const field = source.slice(source.indexOf("Site (optional)"));
    const inputEnd = field.indexOf("/>");
    expect(field.slice(0, inputEnd)).not.toContain("required");
  });

  it("tells the operator what the value becomes, because the rule is not guessable", () => {
    // "letters, numbers, dot, dash or underscore" is the API's rule. Somebody
    // typing "Plant 1" gets a 422 otherwise, with no way to know why a space
    // was wrong.
    const field = source.slice(source.indexOf("Site (optional)"));
    const inputEnd = field.indexOf("/>");
    const input = field.slice(0, inputEnd);
    expect(input).toMatch(/title=/);
    expect(input).toContain("MQTT topic");
  });

  it("keeps the site between submissions, because machines arrive in batches", () => {
    // The other four fields are cleared on success. Clearing this one would
    // make the common case — several machines at one site — the one that needs
    // retyping every time.
    const body = addMachineBody(source);
    expect(body).toContain('setName("")');
    expect(body).not.toContain('setSite("")');
  });
});
