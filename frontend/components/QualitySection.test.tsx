import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import QualitySection from "./QualitySection";
import type { QualityInspection } from "../lib/phase14-types";

/**
 * The quality table's status column showed nothing, for every row.
 *
 * `factory_simulator` writes exactly three inspection statuses — "Passed",
 * "Failed", "Rework" — and the screen offered "Open", "In Review", "Closed",
 * "Rejected". Nothing in AMP has ever written those four, and nothing reads
 * them. So every real inspection bound a value its own `<select>` did not
 * contain, which React renders as an EMPTY box with no warning. `statusStyle`
 * coloured only the fictional values, so all of them also drew the same yellow
 * pill. A passed inspection and a failed one were indistinguishable.
 *
 * That is the shape of defect only a render catches: the logic is fine, the API
 * is fine, the row is there, and the screen is blank. Same reason
 * LockedModuleView earned the first component test — "renders nothing" fails
 * silently.
 */
function inspection(over: Partial<QualityInspection> = {}): QualityInspection {
  return {
    id: 1,
    inspection_no: "QI-7001",
    work_order_id: 1,
    machine_id: 1,
    inspector: "R. Kulkarni",
    inspected_quantity: 100,
    passed_quantity: 93,
    failed_quantity: 7,
    defect_category: "Burr",
    rework_quantity: 5,
    scrap_quantity: 2,
    status: "Rework",
    notes: "",
    ...over,
  } as QualityInspection;
}

// This suite does not run with vitest `globals`, so testing-library's automatic
// cleanup is not registered. Without this, a second render leaves the first
// render's <select> in the document and every query below reads the stale one —
// which is how this file first "failed": the harness, not the component.
afterEach(cleanup);

function renderWith(inspections: QualityInspection[]) {
  return render(
    <QualitySection
      machines={[{ id: 1, name: "SMT-Printer-01", status: "Running", utilization: 82, downtime: "0 min" }]}
      workOrders={[]}
      productionPlans={[]}
      inspections={inspections}
      analytics={null}
      form={{
        inspection_no: "", work_order_id: "", production_plan_id: "", machine_id: "",
        inspector: "", inspected_quantity: 0, passed_quantity: 0, failed_quantity: 0,
        defect_category: "", rework_quantity: 0, scrap_quantity: 0, status: "Passed", notes: "",
      }}
      setForm={() => {}}
      createInspection={() => {}}
      updateInspection={() => {}}
      generateDefectEscalations={() => {}}
      getMachineName={() => "SMT-Printer-01"}
    />,
  );
}

/** The row's status dropdown — the create form's select is the other one. */
function rowStatusSelect(): HTMLSelectElement {
  const selects = screen.getAllByRole("combobox") as HTMLSelectElement[];
  const row = selects[selects.length - 1];
  expect(row).toBeTruthy();
  return row;
}

describe("QualitySection status dropdown", () => {
  it.each(["Passed", "Failed", "Rework"])(
    "shows %s — the statuses the backend actually writes",
    (status) => {
      renderWith([inspection({ status })]);
      const select = rowStatusSelect();
      // The defect was `select.value === ""` here: a bound value with no matching
      // <option> leaves the box empty.
      expect(select.value).toBe(status);
      expect(select.selectedIndex).toBeGreaterThanOrEqual(0);
    },
  );

  it("still shows a status no version of this screen has heard of", () => {
    renderWith([inspection({ status: "Quarantined" })]);
    expect(rowStatusSelect().value).toBe("Quarantined");
  });

  it("distinguishes a passed inspection from a failed one", () => {
    // Both fell to statusStyle's yellow default before, so the pill carried no
    // information even once the text rendered.
    renderWith([inspection({ status: "Passed" })]);
    const passed = rowStatusSelect().className;
    cleanup();

    renderWith([inspection({ status: "Failed" })]);
    const failed = rowStatusSelect().className;

    expect(passed).toContain("green");
    expect(failed).toContain("red");
    expect(failed).not.toBe(passed);
  });

  it("renders an empty table without throwing", () => {
    renderWith([]);
    expect(screen.getByRole("table")).toBeTruthy();
  });
});
