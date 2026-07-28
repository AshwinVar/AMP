import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Rot guard: every stylesheet in app/ must actually be imported.
 *
 * Next.js only bundles CSS that something imports, so an unimported stylesheet
 * is shipped in the repo, edited in good faith, and never reaches a browser.
 * Five of them had accumulated that way - 1,859 lines across
 * enterprise-dashboard, enterprise-ui, globals-extra, globals-ui and
 * phase28-ui, all superseded by the phase29 shell. None of their class names
 * appeared in any JSX either, so nothing was even half-styled by them; they
 * were simply dead.
 *
 * The failure mode is quiet in both directions: a stylesheet that is never
 * imported produces no error, and neither does one that is deleted after
 * everything stopped importing it. This test makes the first case loud.
 */

const APP = join(__dirname, "..", "app");

/** Every .tsx/.ts source under app/ and components/, concatenated. */
function sources(): string {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(path);
      } else if (/\.tsx?$/.test(entry.name)) {
        out.push(readFileSync(path, "utf8"));
      }
    }
  };
  walk(APP);
  walk(join(__dirname, "..", "components"));
  return out.join("\n");
}

describe("app stylesheets", () => {
  const stylesheets = readdirSync(APP).filter((name) => name.endsWith(".css"));

  it("has stylesheets to check", () => {
    // If this ever hits zero the loop below would pass vacuously.
    expect(stylesheets.length).toBeGreaterThan(0);
  });

  for (const sheet of stylesheets) {
    it(`${sheet} is imported by something`, () => {
      const code = sources();
      const cssFiles = readdirSync(APP)
        .filter((n) => n.endsWith(".css"))
        .map((n) => readFileSync(join(APP, n), "utf8"))
        .join("\n");

      // Either a JS/TS import, or an @import from another stylesheet.
      const importedByCode = new RegExp(`import\\s+["'][^"']*${sheet}["']`).test(code);
      const importedByCss = new RegExp(`@import\\s+["'][^"']*${sheet}["']`).test(cssFiles);

      expect(
        importedByCode || importedByCss,
        `${sheet} is not imported anywhere — Next.js will not bundle it, so it ` +
          `ships in the repo and never reaches a browser. Import it or delete it.`,
      ).toBe(true);
    });
  }
});
