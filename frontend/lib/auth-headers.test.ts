import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * One place builds the request's identity headers.
 *
 * lib/api.ts getAuthHeaders / getDownloadHeaders attach Authorization AND, while
 * the founder previews a customer, X-Tenant. Two screens rebuilt the header by
 * hand — `{ Authorization: \`Bearer ${localStorage.getItem("token")}\` }` — and
 * dropped the preview, so a founder importing a customer's CSV wrote it into the
 * platform workspace (EnterpriseInventory.import.test.tsx is the behaviour).
 *
 * A second copy of an identity rule is how this happens, so the rule is: no
 * source outside lib/api.ts writes an Authorization header. A new raw fetch must
 * take its headers from lib/api. The OEM portal has its own principal and its
 * own client (lib/oem.ts), so it is the one named exception.
 */

const ROOT = join(__dirname, "..");
const SCAN = ["app", "components", "lib"];
const ALLOWED = new Set([join("lib", "api.ts"), join("lib", "oem.ts")]);

function sources(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) {
        if (name !== "node_modules" && name !== ".next") walk(path);
      } else if (/\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name)) {
        out.push(path);
      }
    }
  };
  SCAN.forEach((d) => walk(join(ROOT, d)));
  return out;
}

const AUTH_HEADER = /\bAuthorization\s*:/;

describe("identity headers have one home", () => {
  it("the scan reaches lib/api.ts and finds the header there (a guard that finds nothing proves nothing)", () => {
    const api = readFileSync(join(ROOT, "lib", "api.ts"), "utf8");
    expect(AUTH_HEADER.test(api)).toBe(true);
    expect(sources().length).toBeGreaterThan(50);
  });

  it("no other source builds an Authorization header by hand", () => {
    const offenders = sources()
      .map((p) => relative(ROOT, p).split("/").join(sep))
      .filter((rel) => !ALLOWED.has(rel))
      .filter((rel) => AUTH_HEADER.test(readFileSync(join(ROOT, rel), "utf8")));
    expect(offenders).toEqual([]);
  });
});
