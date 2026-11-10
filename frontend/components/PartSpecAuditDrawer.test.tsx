import { describe, expect, it } from "vitest";

import { auditCsvRows, isCreate } from "./PartSpecAuditDrawer";

/**
 * The two pure decisions behind the audit drawer, tested away from React.
 *
 *   isCreate      — a create records every field with old=null; a revise does
 *                   not. The backend stores no separate "action", so the shape
 *                   of the change set is the only signal, and getting it wrong
 *                   would label a brand-new part "Revised" (or vice versa).
 *   auditCsvRows  — the trail flattened one row per changed field, which is what
 *                   a spreadsheet can sort and filter. A per-entry blob could not.
 */
describe("part-spec audit helpers", () => {
  it("reads a create when every field's old is null", () => {
    expect(
      isCreate({
        changes: {
          part_name: { old: null, new: "Ele clip" },
          price_per_piece: { old: null, new: 0.09 },
        },
      }),
    ).toBe(true);
  });

  it("reads a revise when any field has a non-null old", () => {
    expect(isCreate({ changes: { price_per_piece: { old: 0.09, new: 0.12 } } })).toBe(false);
  });

  it("treats an empty change set as neither (defensive)", () => {
    expect(isCreate({ changes: {} })).toBe(false);
  });

  it("flattens the trail one row per changed field", () => {
    const rows = auditCsvRows([
      {
        part_code: "ELE-CLIP",
        part_name: "Ele clip",
        actor: "ashwin",
        at: "2026-06-01T04:30:00",
        changes: {
          price_per_piece: { old: 0.09, new: 0.12 },
          effective_from: { old: "2026-01-01", new: "2026-06-01" },
        },
      },
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0]["Part code"]).toBe("ELE-CLIP");
    expect(rows[0].Actor).toBe("ashwin");
    // The old -> new pairs survive the flattening, in field order.
    expect(rows.map((r) => r.Old)).toEqual([0.09, "2026-01-01"]);
    expect(rows.map((r) => r.New)).toEqual([0.12, "2026-06-01"]);
  });
});
