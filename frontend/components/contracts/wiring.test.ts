import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * STRUCTURAL GUARD for the two screens that are not unit-rendered (ADR-0020).
 *
 * The factory dashboard (app/dashboard/page.tsx, ~2,900 lines) and the OEM
 * portal page are exercised by Playwright, not by vitest. The contract
 * components ARE unit-tested, which proves nothing if the pages stop rendering
 * them, render them outside the role and plan gate, or grow a second, private
 * contract screen. So this reads the page sources and asserts the wiring, and
 * each assertion first proves it found its target.
 */

const ROOT = join(__dirname, "..", "..");
// CRLF normalised: git writes these files with CRLF on Windows, and a pattern
// spelled with a bare newline would silently match nothing there.
const read = (rel: string) => readFileSync(join(ROOT, rel), "utf8").replace(/\r\n/g, "\n");
const count = (text: string, needle: string) => text.split(needle).length - 1;

describe("the factory dashboard renders service contracts through the gate", () => {
  const page = read("app/dashboard/page.tsx");

  it("imports the screen once", () => {
    expect(count(page, 'import ServiceContracts from "../../components/ServiceContracts";')).toBe(1);
  });

  it("renders it only inside renderSection(\"contracts\"), which applies role and plan", () => {
    const section = /\{renderSection\("contracts", \(\s*<ServiceContracts \/>\s*\)\)\}/;
    expect(section.test(page)).toBe(true);
    expect(count(page, "<ServiceContracts")).toBe(1);
    // The gate this relies on is still the one renderSection applies.
    expect(page).toMatch(/function renderSection\(viewKey: string, node: React\.ReactNode\) \{\s*if \(activeView !== viewKey\) return null;\s*if \(!canRoleSeeView\(viewKey, role, isFounder\)\)/);
  });
});

describe("the OEM portal renders service contracts with the principal's capabilities", () => {
  const page = read("app/oem/page.tsx");

  it("imports the screen once and passes identity.capabilities, never a constant", () => {
    expect(count(page, 'import OemContracts from "../../components/OemContracts";')).toBe(1);
    expect(count(page, "<OemContracts capabilities={identity.capabilities} fleet={machines} />")).toBe(1);
  });

  it("renders it only once the server has said who is signed in", () => {
    const at = page.indexOf("<OemContracts");
    const gate = page.lastIndexOf("{identity && (", at);
    expect(at).toBeGreaterThan(0);
    expect(gate).toBeGreaterThan(0);
    expect(page.slice(gate, at)).not.toMatch(/\n {6}\)\}\n/);
  });
});

describe("one contract workspace for both parties", () => {
  it("each side renders the shared workspace exactly once, over its own transport", () => {
    const oem = read("components/OemContracts.tsx");
    const factory = read("components/ServiceContracts.tsx");
    expect(count(oem, "<ContractWorkspace")).toBe(1);
    expect(count(factory, "<ContractWorkspace")).toBe(1);
    expect(oem).toMatch(/api=\{oemContractsApi\}/);
    expect(factory).toMatch(/api=\{serviceContractsApi\}/);
  });

  it("no screen decides acceptance validity itself", () => {
    for (const rel of ["components/OemContracts.tsx", "components/ServiceContracts.tsx",
                       "components/contracts/ContractWorkspace.tsx",
                       "components/contracts/AcceptancePanel.tsx"]) {
      const src = read(rel);
      expect(src.length, rel).toBeGreaterThan(0);
      // Validity is the server's `valid` flag, read through lib/contracts.
      expect(src, rel).not.toMatch(/content_hash\s*===|revision\s*===\s*statement\.revision\s*&&/);
    }
  });
});
