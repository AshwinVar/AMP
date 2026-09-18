import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Every mutation in frontend/mutate-*.mjs still applies to the code.
 *
 * A UI mutation harness breaks one guarantee at a time (a withheld value shown
 * as its zero, a refused request reported as success) and asserts that a suite
 * goes red. It finds the code to break by its text. When a change rewrites that
 * text, the mutation matches nothing and the harness prints SKIP, which reads
 * almost exactly like a pass in a wall of output. It prints that only when
 * somebody runs the harness, and CI does not.
 *
 * #612 moved AddConnectedEquipment's private `detailOf(err)` into lib/api as
 * `errorDetail(err)`, a correct refactor that stranded two mutations: "a refused
 * lookup is swallowed into a generic message" and "a refused confirmation
 * reports success anyway". backend/test_mutation_anchors_apply.py does this for
 * the Python harnesses. This does it for these two, in seconds, on every push.
 *
 * The harnesses run their mutations at top level, so they cannot be imported.
 * Only the MUTATIONS array literal is read and evaluated.
 */

const ROOT = join(__dirname, "..");

type Edit = { file?: string; from: string };
type Mutation = { label: string; file?: string; from?: string; edits?: Edit[] };

/** The source text of `const MUTATIONS = [...]`, brackets matched, strings and
 * comments skipped (a quote or a bracket inside either must not end the scan). */
export function mutationsLiteral(src: string): string {
  const start = src.indexOf("const MUTATIONS = [");
  if (start < 0) return "";
  const open = src.indexOf("[", start);
  let depth = 0;
  let quote: string | null = null;
  for (let i = open; i < src.length; i++) {
    const c = src[i];
    if (quote) {
      if (c === "\\") i++;
      else if (c === quote) quote = null;
      continue;
    }
    if (c === "/" && src[i + 1] === "/") {
      i = src.indexOf("\n", i);
      if (i < 0) return "";
      continue;
    }
    if (c === "/" && src[i + 1] === "*") {
      i = src.indexOf("*/", i) + 1;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") quote = c;
    else if (c === "[") depth++;
    else if (c === "]" && --depth === 0) return src.slice(open, i + 1);
  }
  return "";
}

function mutationsOf(harness: string): Mutation[] {
  const literal = mutationsLiteral(readFileSync(join(ROOT, harness), "utf8"));
  return literal ? (new Function(`return ${literal}`)() as Mutation[]) : [];
}

const harnesses = readdirSync(ROOT).filter((f) => /^mutate-.*\.mjs$/.test(f));

describe("every UI mutation still applies", () => {
  it("the harnesses were found, and each one's mutations were read", () => {
    expect(harnesses.length).toBeGreaterThanOrEqual(2);
    for (const h of harnesses) expect([h, mutationsOf(h).length > 0]).toEqual([h, true]);
  });

  it("every anchor names exactly one place in its file", () => {
    const offenders: string[] = [];
    for (const h of harnesses) {
      for (const m of mutationsOf(h)) {
        for (const e of m.edits ?? [{ file: m.file, from: m.from ?? "" }]) {
          const file = e.file ?? m.file ?? "";
          let text = "";
          try {
            text = readFileSync(join(ROOT, file), "utf8").replace(/\r\n/g, "\n");
          } catch {
            offenders.push(`${h}: [${m.label}] ${file} does not exist`);
            continue;
          }
          const hits = e.from ? text.split(e.from).length - 1 : 0;
          if (hits !== 1) offenders.push(`${h}: [${m.label}] matches ${hits}x in ${file}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("the extractor is not fooled by a quote or a bracket in a comment or a string", () => {
    const src = [
      "const MUTATIONS = [",
      "  // the customer's bracket ] is not the end",
      '  { label: "a", file: "x.ts", from: "arr[0]", to: "arr[1]" },',
      "  /* nor is this ] */",
      "];",
      "run();",
    ].join("\n");
    const parsed = new Function(`return ${mutationsLiteral(src)}`)() as Mutation[];
    expect(parsed.map((m) => m.from)).toEqual(["arr[0]"]);
  });
});
