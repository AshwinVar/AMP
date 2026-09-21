import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Rot guard: the dashboard's inline write controls sit behind the role the
 * server requires.
 *
 * app/dashboard/page.tsx is one 3,000-line component with no render test, and
 * four of its controls were offered to every role while their routes refused
 * all but one: the Add Machine form and the Delete Machine button (POST /
 * DELETE /machines: Admin), the machine card's status select (PATCH
 * /machines/{id}/status: Admin or Supervisor) and the Shift Performance Entry
 * form (POST /shifts: Admin or Supervisor) — the first two on the landing view
 * every role sees. Every other section is wired through `isAdmin ? … :
 * undefined` props; these four were rendered by a bare `activeView ===`
 * block and never got the gate.
 *
 * The check is textual, like lib/dashboard-write-errors.test.ts: for each
 * control, the nearest opening gate before it must come AFTER the block that
 * renders it, so a gate elsewhere in the file cannot satisfy it by accident.
 */

const PAGE = join(__dirname, "..", "app", "dashboard", "page.tsx");

/** True when `gate` opens between the start of `block` and `control`. */
export function gatedWithin(source: string, block: string, control: string, gate: string): boolean {
  const at = source.indexOf(control);
  if (at < 0) return false;
  const before = source.slice(0, at);
  const blockAt = before.lastIndexOf(block);
  const gateAt = before.lastIndexOf(gate);
  return blockAt >= 0 && gateAt > blockAt;
}

const CONTROLS: Array<[string, string, string, string]> = [
  // [what, the block that renders it, the control, the gate the route requires]
  ["the Add Machine form (POST /machines: Admin)",
   'activeView === "machines") && (', "onSubmit={addMachine}", "{isAdmin && ("],
  ["the machine card's status select (PATCH /machines/{id}/status: Admin or Supervisor)",
   "{machines.map((machine) => {", "updateMachineStatus(machine.id, e.target.value)", "{isAdminOrSupervisor && ("],
  ["the Delete Machine button (DELETE /machines/{id}: Admin)",
   "updateMachineStatus(machine.id, e.target.value)", "deleteMachine(machine.id)", "{isAdmin && ("],
  ["the Shift Performance Entry form (POST /shifts: Admin or Supervisor)",
   'activeView === "shifts") && (', "onSubmit={addShift}", "{isAdminOrSupervisor && ("],
];

describe("the dashboard offers its inline writes only to the role the route requires", () => {
  const source = readFileSync(PAGE, "utf8");

  it.each(CONTROLS)("%s is gated", (_what, block, control, gate) => {
    expect(source.includes(control), `control not found: ${control}`).toBe(true);
    expect(gatedWithin(source, block, control, gate)).toBe(true);
  });

  it("passes the GMATS inventory the Supervisor gate, not only the Admin one", () => {
    expect(source).toContain('<GmatsInventory tenant="GMATS" isAdmin={isAdmin} canWrite={isAdminOrSupervisor} />');
  });

  it("would fail on the code as it was", () => {
    // Guards the guard: the gate has to open INSIDE the block. One before the
    // block (the CsvImportButton's, in the old code) does not count.
    const old = '{activeView === "machines" && isAdmin && (<CsvImportButton />)}\n' +
                '{(activeView === "overview" || activeView === "machines") && (\n' +
                "  <form onSubmit={addMachine}>";
    expect(gatedWithin(old, 'activeView === "machines") && (', "onSubmit={addMachine}", "isAdmin && (")).toBe(false);
    const fixed = '{(activeView === "overview" || activeView === "machines") && (\n' +
                  "  {isAdmin && (\n  <form onSubmit={addMachine}>";
    expect(gatedWithin(fixed, 'activeView === "machines") && (', "onSubmit={addMachine}", "{isAdmin && (")).toBe(true);
  });
});
