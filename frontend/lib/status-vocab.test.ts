import { describe, expect, it } from "vitest";

import vocab from "./status-vocab.json";
import { knownValues, statusOptions } from "./status-vocab";

/**
 * A controlled `<select>` whose option list is missing the bound value renders
 * an EMPTY box — no selection, no React warning, no error. Six screens had
 * hardcoded lists that had drifted from what the backend writes; seeding a real
 * factory and counting, 42 of 123 rows (34%) rendered blank, including 12 of 12
 * quality inspections.
 *
 * The property that makes that unrepeatable is not "the list is correct today"
 * — a list can always drift again. It is that the list is a COMPLEMENT, not a
 * whitelist: whatever the row holds is appended if it is not already there, so
 * an unrecognised value is DISPLAYED rather than blanked. These tests pin that
 * property, not the contents.
 *
 * backend/test_status_vocabulary_parity.py checks the other direction from the
 * same JSON file — that nothing the backend writes is missing from it.
 */
const PAIRS = Object.entries(vocab).flatMap(([model, fields]) =>
  Object.keys(fields).map((field) => [model, field] as const),
);

describe("statusOptions", () => {
  it("can render every value the vocabulary knows about", () => {
    // The defect, stated as a property. If this fails, some screen shows a blank box.
    for (const [model, field] of PAIRS) {
      for (const value of knownValues(model, field)) {
        expect(
          statusOptions(model, field, value),
          `${model}.${field} cannot show ${value}`,
        ).toContain(value);
      }
    }
  });

  it("appends a value nobody enumerated rather than dropping it", () => {
    // The whole point: the next vocabulary the backend invents still renders.
    const options = statusOptions("MaintenanceTask", "status", "Quarantined");
    expect(options).toContain("Quarantined");
    expect(options[options.length - 1]).toBe("Quarantined");
  });

  it("does not offer a system-only value when the row does not hold it", () => {
    // ai/agents.approve_action transitions only `if item.status == "Proposed"`,
    // so a human who sets Proposed by hand creates a task whose approval can
    // never fire. Displayable, never selectable.
    expect(statusOptions("MaintenanceTask", "status", "Open")).not.toContain("Proposed");
    expect(statusOptions("MaintenanceTask", "status", "Proposed")).toContain("Proposed");
  });

  it("never duplicates a value already in the list", () => {
    const options = statusOptions("MaintenanceTask", "status", "Open");
    expect(options.filter((o) => o === "Open")).toHaveLength(1);
    expect(new Set(options).size).toBe(options.length);
  });

  it("returns the plain list for a row with no value yet", () => {
    for (const current of [undefined, null, "", "   "]) {
      expect(statusOptions("Supplier", "status", current)).toEqual(
        ["Active", "On Hold", "Inactive"],
      );
    }
  });

  it("trims a padded value instead of adding a second entry for it", () => {
    expect(statusOptions("Supplier", "status", "  Active  ")).toEqual(
      ["Active", "On Hold", "Inactive"],
    );
  });

  it("still shows the value when the model or field is unknown to the bundle", () => {
    // Degrading to "shows the truth" beats degrading to "shows nothing", even
    // when the vocabulary file has no entry at all.
    expect(statusOptions("NotAModel", "status", "Whatever")).toEqual(["Whatever"]);
    expect(statusOptions("Supplier", "not_a_field", "Whatever")).toEqual(["Whatever"]);
    expect(statusOptions("NotAModel", "status")).toEqual([]);
  });
});

describe("the vocabulary itself", () => {
  it("covers enough to be worth having", () => {
    // A selector that collapses to nothing agrees with every assertion above.
    expect(PAIRS.length).toBeGreaterThanOrEqual(11);
    const values = new Set(PAIRS.flatMap(([m, f]) => knownValues(m, f)));
    expect(values.size).toBeGreaterThanOrEqual(30);
  });

  it("keeps options and systemOnly disjoint, and every entry non-empty", () => {
    for (const [model, fields] of Object.entries(vocab)) {
      for (const [field, spec] of Object.entries(fields)) {
        expect(spec.options.length, `${model}.${field} offers nothing`).toBeGreaterThan(0);
        const overlap = spec.options.filter((o: string) => spec.systemOnly.includes(o));
        expect(overlap, `${model}.${field}`).toEqual([]);
        expect(new Set(spec.options).size).toBe(spec.options.length);
      }
    }
  });
});
