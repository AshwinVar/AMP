import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

import { CURRENCY, money } from "./money";

/**
 * Rot guard: the currency symbol is written down in ONE place, lib/money.ts.
 *
 * lib/money.ts says so in its first line, and backend/test_currency_single.py
 * keeps it equal to the backend's. Nothing checked the claim. Nine components
 * and lib/modules.ts wrote "£" themselves, and four of them formatted the amount
 * themselves too, with no thousands separator. The Cost KPI on the enterprise
 * page, the Costing card's manual cost, and the SaaS page's recurring revenue and
 * monthly fees read "£49740" where every other card reads "£49,740" (money()).
 * That is the inconsistency backend/report_generator.py was already corrected
 * for.
 *
 * Comments may name the symbol (they explain it); code may not.
 */

const ROOT = join(__dirname, "..");

function sourceFiles(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== "node_modules" && !entry.name.startsWith(".")) walk(path);
      } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
        out.push(path);
      }
    }
  };
  for (const dir of ["app", "components", "lib"]) walk(join(ROOT, dir));
  return out;
}

/** The code on a line, with // and single-line /* *\/ comments removed, and
 * nothing at all for a line inside a block comment. */
export function codeOf(line: string, inBlock: { open: boolean }): string {
  let rest = line;
  let code = "";
  while (rest.length) {
    if (inBlock.open) {
      const end = rest.indexOf("*/");
      if (end < 0) return code;
      inBlock.open = false;
      rest = rest.slice(end + 2);
      continue;
    }
    const block = rest.indexOf("/*");
    const lineComment = rest.search(/(^|[^:])\/\//);
    if (lineComment >= 0 && (block < 0 || lineComment < block)) {
      return code + rest.slice(0, lineComment + (rest[lineComment] === "/" ? 0 : 1));
    }
    if (block < 0) return code + rest;
    code += rest.slice(0, block);
    inBlock.open = true;
    rest = rest.slice(block + 2);
  }
  return code;
}

function literalSymbols(): string[] {
  const offenders: string[] = [];
  for (const path of sourceFiles()) {
    const rel = relative(ROOT, path).split("\\").join("/");
    if (rel === "lib/money.ts") continue;
    const inBlock = { open: false };
    readFileSync(path, "utf8").split(/\r?\n/).forEach((line, i) => {
      if (codeOf(line, inBlock).includes(CURRENCY)) offenders.push(`${rel}:${i + 1}: ${line.trim()}`);
    });
  }
  return offenders;
}

describe("the currency symbol has one home", () => {
  it("no source file outside lib/money.ts writes the symbol itself", () => {
    expect(literalSymbols()).toEqual([]);
  });

  it("the scan reads real files (a guard that reads nothing passes everything)", () => {
    const files = sourceFiles().map((p) => relative(ROOT, p).split("\\").join("/"));
    expect(files).toContain("components/MoneyStorySnapshot.tsx");
    expect(files).toContain("lib/money.ts");
    expect(files.some((f) => f.endsWith(".test.ts"))).toBe(false);
  });

  it("comments may name the symbol; code may not", () => {
    const scan = (lines: string[]) => {
      const inBlock = { open: false };
      return lines.filter((l) => codeOf(l, inBlock).includes(CURRENCY));
    };
    // The fixtures are BUILT FROM CURRENCY, not written with a symbol.
    //
    // They used to spell out "£". When the platform moved to rupees this test
    // kept passing the empty-result half and silently stopped proving the other
    // half: scan() looks for CURRENCY, the fixtures contained a pound sign, so
    // "code may not name the symbol" was asserted against three lines that
    // named no symbol at all. A scanner self-test has to follow the symbol it
    // scans for.
    const C = CURRENCY;
    expect(scan([
      "// shows " + C + " when a rate is set",
      "/* a " + C + " figure */ const x = 1;",
      "/**",
      " * " + C + "/good-unit rate",
      " */",
      'const url = "https://example.com"; // ' + C + " note",
    ])).toEqual([]);
    expect(scan([
      "const rate = (n: number) => `" + C + "${n}`;",
      "<span>" + C + "{rate}</span>",
      '/* note */ const s = "' + C + '0";',
    ])).toHaveLength(3);
  });

  it("money() groups in lakhs, as an Indian plant's own ledger does", () => {
    // 49,740 groups identically under either convention, which is why it could
    // not catch the backend and the frontend disagreeing. 1400000 can.
    expect(money(49740)).toBe(`${CURRENCY}49,740`);
    expect(money(100000)).toBe(`${CURRENCY}1,00,000`);
    expect(money(1400000)).toBe(`${CURRENCY}14,00,000`);
    expect(money(-1400000)).toBe(`${CURRENCY}-14,00,000`);
  });
});
