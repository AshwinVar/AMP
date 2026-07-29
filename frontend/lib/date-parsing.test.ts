import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Rot guard: nothing in the UI may parse an API date string with `new Date(…)`.
 *
 * The backend sends naive UTC timestamps and bare calendar dates, and the two
 * are parsed in opposite, both-wrong ways by the platform (see lib/apiDate.ts).
 * `new Date(iso)` therefore looks correct, type-checks, renders something
 * plausible, and is silently wrong by the viewer's UTC offset — which is zero
 * on a developer's UTC box and on CI. Nothing catches it downstream.
 *
 * Twenty-nine call sites had it. Two of them (CoverageSnapshot, PartRunwayDrawer)
 * carried a hand-rolled `iso + "T00:00:00"` patch, so the day-shift had already
 * been found once and fixed locally without the other twenty-seven being looked
 * at. That is precisely the shape of rot this guards against.
 *
 * `new Date()` with no argument is fine — that is "now", not parsing.
 */

const ROOT = join(__dirname, "..");
const SCANNED = ["app", "components"];

/**
 * Rot-proof by construction: an empty allowlist. A new violation cannot be
 * waved through by adding a line here without someone noticing in review.
 */
const ALLOWED: string[] = [];

type Hit = { file: string; line: number; text: string };

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) walk(path, out);
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(path);
  }
  return out;
}

/** `new Date(` followed by anything other than an immediate `)`. */
const PARSING_DATE = /new\s+Date\s*\(\s*(?!\))/;

function violations(): Hit[] {
  const hits: Hit[] = [];
  for (const dir of SCANNED) {
    for (const file of walk(join(ROOT, dir))) {
      const rel = relative(ROOT, file).replace(/\\/g, "/");
      if (ALLOWED.includes(rel)) continue;
      readFileSync(file, "utf8")
        .split("\n")
        .forEach((text, i) => {
          if (PARSING_DATE.test(text)) hits.push({ file: rel, line: i + 1, text: text.trim() });
        });
    }
  }
  return hits;
}

describe("API date parsing goes through lib/apiDate", () => {
  it("has files to scan", () => {
    // Guards the guard: a bad path would make the check below pass vacuously.
    expect(SCANNED.flatMap((d) => walk(join(ROOT, d))).length).toBeGreaterThan(20);
  });

  it("detects the pattern it is looking for", () => {
    // And guards the regex: if this stopped matching, the scan would be silent.
    expect(PARSING_DATE.test("const d = new Date(iso);")).toBe(true);
    expect(PARSING_DATE.test('new Date(iso + "T00:00:00")')).toBe(true);
    expect(PARSING_DATE.test("new Date(r.at).toLocaleString()")).toBe(true);
    // ...and does not fire on "now", which is not parsing.
    expect(PARSING_DATE.test("const now = new Date();")).toBe(false);
    expect(PARSING_DATE.test("created_at: new Date().toISOString(),")).toBe(false);
  });

  it("finds no component parsing a date string by hand", () => {
    const hits = violations();
    expect(
      hits,
      hits.length
        ? `new Date(<string>) parses a naive backend timestamp as LOCAL time and a ` +
          `bare date as UTC midnight — both wrong. Use parseApiDate from lib/apiDate:\n` +
          hits.map((h) => `  ${h.file}:${h.line}  ${h.text}`).join("\n")
        : "",
    ).toEqual([]);
  });
});
