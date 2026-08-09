import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import BomViewer from "./BomViewer";

/**
 * A part now spans SEVERAL rows, one per component.
 *
 * The recipe book used to be a module-level dict with exactly one raw material
 * per part (ADR-0013), so `/bom` returned one row per part and this table keyed
 * on `key={row.part_number}`. A real bill of materials has many components, and
 * the moment the endpoint returned two rows for one part that key collided.
 *
 * React does not throw on duplicate keys — it warns to the console and then
 * reconciles siblings wrongly, which shows up as rows swapping or stale content
 * after a re-render. That is precisely the kind of failure a lib/ test cannot
 * see (the grouping is correct in isolation) and a passing build does not catch
 * (it type-checks and renders fine on first paint).
 *
 * So these tests assert what the user sees: every component listed, each part
 * named once, and no duplicate-key warning.
 */

interface Row {
  id: number;
  part_number: string;
  revision: string;
  active: boolean;
  effective_from: string | null;
  finished_goods_code: string;
  finished_goods_name: string;
  component_id: number | null;
  component_code: string;
  component_name: string;
  quantity_per_unit: number;
  unit: string;
  component_stock: number | null;
  component_reorder_level: number | null;
}

function row(over: Partial<Row> = {}): Row {
  return {
    id: 1,
    part_number: "SHAFT-001",
    revision: "A",
    active: true,
    effective_from: null,
    // Deliberately NOT "FG-SHAFT-001": a /SHAFT-001/ matcher would then hit the
    // finished-goods cell too and every count below would be doubled - a
    // fixture problem that reads exactly like a rendering bug.
    finished_goods_code: "FG-9001",
    finished_goods_name: "Finished shaft",
    component_id: 1,
    component_code: "RM-STEEL-001",
    component_name: "Steel bar",
    quantity_per_unit: 2,
    unit: "kg",
    component_stock: 100,
    component_reorder_level: 10,
    ...over,
  };
}

const rows = vi.hoisted(() => ({ value: [] as unknown[] }));
vi.mock("../lib/api", () => ({
  apiGet: vi.fn(async () => rows.value),
}));

let warnings: string[] = [];

beforeEach(() => {
  warnings = [];
  vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    warnings.push(args.map(String).join(" "));
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("BomViewer", () => {
  it("lists every component of a multi-component part", async () => {
    rows.value = [
      row({ component_id: 1, component_code: "RM-STEEL-001", component_name: "Steel bar", quantity_per_unit: 1.5 }),
      row({ component_id: 2, component_code: "RM-SEAL-002", component_name: "Seal", quantity_per_unit: 2, unit: "pc" }),
      row({ component_id: 3, component_code: "RM-PAINT-3", component_name: "Paint", quantity_per_unit: 0.25, unit: "l" }),
    ];
    render(<BomViewer />);

    await waitFor(() => expect(screen.getByText("Steel bar")).toBeTruthy());
    // All three components are visible — the old one-row-per-part shape could
    // only ever show the first.
    expect(screen.getByText("Seal")).toBeTruthy();
    expect(screen.getByText("Paint")).toBeTruthy();

    // The part is named ONCE, not once per component.
    expect(screen.getAllByText(/SHAFT-001/).length).toBe(1);
  });

  it("does not emit a duplicate-key warning for a multi-component part", async () => {
    rows.value = [
      row({ component_id: 1, component_code: "A-1" }),
      row({ component_id: 2, component_code: "A-2" }),
    ];
    render(<BomViewer />);
    await waitFor(() => expect(screen.getByText("A-1")).toBeTruthy());

    const duplicateKey = warnings.filter((w) => /same key|duplicate key/i.test(w));
    expect(duplicateKey).toEqual([]);
  });

  it("keeps two parts apart even when they share a component", async () => {
    rows.value = [
      row({ id: 1, part_number: "SHAFT-001", component_id: 1, component_code: "RM-STEEL-001" }),
      row({ id: 2, part_number: "BEAR-003", component_id: 2, component_code: "RM-STEEL-001", finished_goods_code: "", finished_goods_name: "" }),
    ];
    render(<BomViewer />);
    await waitFor(() => expect(screen.getByText(/SHAFT-001/)).toBeTruthy());
    expect(screen.getByText(/BEAR-003/)).toBeTruthy();
    // Both rows reference the same component code; neither row is dropped.
    expect(screen.getAllByText("RM-STEEL-001").length).toBe(2);
  });

  it("shows a BOM that has no components at all", async () => {
    // A part that only produces is a real case — the dict this replaced had two
    // of six with no finished good and one with no raw material. Dropping it
    // would hide it from the only screen that can fix it.
    rows.value = [
      row({ component_id: null, component_code: "", component_name: "", quantity_per_unit: 0, component_stock: null, component_reorder_level: null }),
    ];
    render(<BomViewer />);
    await waitFor(() => expect(screen.getByText(/SHAFT-001/)).toBeTruthy());
    expect(screen.getByText(/only produces/i)).toBeTruthy();
  });

  it("distinguishes a component this workspace does not stock from an empty shelf", async () => {
    rows.value = [
      row({ component_id: 1, component_code: "KNOWN", component_stock: 0, component_reorder_level: 5 }),
      row({ component_id: 2, component_code: "UNKNOWN", component_stock: null, component_reorder_level: null }),
    ];
    render(<BomViewer />);
    await waitFor(() => expect(screen.getByText("Stockout")).toBeTruthy());
    // "not stocked" is a different, more urgent state than "healthy": the recipe
    // names an item the workspace has never held, so a completion will silently
    // consume nothing for that line.
    expect(screen.getByText("Not stocked")).toBeTruthy();
    expect(screen.queryByText("Healthy")).toBeNull();
  });

  it("says so plainly when there are no recipes yet", async () => {
    rows.value = [];
    render(<BomViewer />);
    await waitFor(() =>
      expect(screen.getByText(/No bills of materials defined yet/i)).toBeTruthy(),
    );
    // ...and explains the consequence, because an empty table on its own reads
    // as "nothing to see" rather than "completions move no stock".
    expect(screen.getByText(/moves no stock/i)).toBeTruthy();
  });

  it("renders the quantity a table cell can actually show", async () => {
    rows.value = [row({ quantity_per_unit: 1.5, unit: "kg" })];
    render(<BomViewer />);
    // 1.5 is the ordinary case the old integer-only dict could not express.
    await waitFor(() => {
      const table = screen.getByRole("table");
      expect(within(table).getByText(/1\.5 kg/)).toBeTruthy();
    });
  });
});
