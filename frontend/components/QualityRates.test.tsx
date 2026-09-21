import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * A fail rate nobody measured is a dash, and every tile says which week it means.
 *
 * "Fail Rate" was published on three bases at once. The Quality view's tiles and
 * the digital twin's tile pooled EVERY inspection ever recorded; the intel card
 * pooled the last seven calendar dates; the machine cockpit pooled the canonical
 * rolling week. A plant that ran badly last quarter and cleanly this one showed
 * 21% on two screens and 1% on a third, and none of them said which week.
 *
 * And a rate over nothing read 0% — the BEST value on a fail-rate scale — so a
 * plant that had stopped inspecting rendered as a plant making nothing wrong.
 * The backend now sends null with `measured: false` and the window it used
 * (quality_contract); these screens have to render that, not `0%`.
 */

const apiGet = vi.fn();
vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

import DigitalTwinSection from "./DigitalTwinSection";
import QualitySection from "./QualitySection";
import QualitySnapshot from "./QualitySnapshot";
import type { QualityAnalytics } from "../lib/phase14-types";
import type { FactoryCommandCenter } from "../lib/phase16-types";

beforeEach(() => apiGet.mockReset());
afterEach(cleanup);

function analytics(over: Partial<QualityAnalytics> = {}): QualityAnalytics {
  return {
    total_inspections: 3,
    inspected_quantity: 1000,
    passed_quantity: 990,
    failed_quantity: 10,
    rework_quantity: 4,
    scrap_quantity: 2,
    pass_rate: 99,
    fail_rate: 1,
    measured: true,
    window: "last 7 days",
    days: 7,
    defect_counts: { Scratch: 10 },
    machine_failures: { 1: 10 },
    ...over,
  };
}

function renderQuality(a: QualityAnalytics | null) {
  return render(
    <QualitySection
      machines={[{ id: 1, name: "SMT-Printer-01", status: "Running", utilization: 82, downtime: "0 min" }]}
      workOrders={[]}
      productionPlans={[]}
      inspections={[]}
      analytics={a}
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

/** A KPI tile renders its title and value inside one box. */
const tile = (title: string) => screen.getByText(title).parentElement as HTMLElement;

describe("the Quality view's rate tiles", () => {
  it("shows the measured rates and names the window they cover", () => {
    renderQuality(analytics());
    expect(tile("Pass Rate").textContent).toContain("99%");
    expect(tile("Fail Rate").textContent).toContain("1%");
    // The tiles used to carry no basis at all while summing the whole register.
    expect(screen.getByText(/Inspection totals · last 7 days/)).toBeTruthy();
  });

  it("shows a dash, not 0%, when the window inspected no units", () => {
    renderQuality(analytics({
      total_inspections: 1, inspected_quantity: 0, passed_quantity: 0, failed_quantity: 0,
      pass_rate: null, fail_rate: null, measured: false, defect_counts: {}, machine_failures: {},
    }));
    expect(tile("Pass Rate").textContent).toContain("—");
    expect(tile("Fail Rate").textContent).toContain("—");
    expect(tile("Fail Rate").textContent).not.toContain("0%");
    // ...and it says why, rather than leaving a bare dash to be read as an outage.
    expect(screen.getByText("no units inspected in this window")).toBeTruthy();
    // The COUNTS over nothing are still zero — only the rates are unmeasured.
    // Scoped to the KPI row: "Inspected" is also a column in the table below.
    const grid = screen.getByText(/Inspection totals/).parentElement!
      .nextElementSibling as HTMLElement;
    expect(within(grid).getByText("Inspected").parentElement!.textContent).toContain("0");
  });

  it("renders a dash before the payload arrives, never a fabricated 0%", () => {
    renderQuality(null);
    expect(tile("Fail Rate").textContent).toContain("—");
  });
});

describe("the digital twin's Quality Fail tile", () => {
  function renderTwin(cc: FactoryCommandCenter | null) {
    apiGet.mockResolvedValue([]);
    return render(
      <DigitalTwinSection
        machines={[]}
        nodes={[]}
        commandCenter={cc}
        form={{ machine_id: "", node_name: "", node_type: "machine", x_position: 0, y_position: 0, width: 10, height: 10, zone: "" }}
        setForm={() => {}}
        createNode={() => {}}
        updateNode={() => {}}
        autoGenerateLayout={() => {}}
      />,
    );
  }

  const centre = (over: Partial<FactoryCommandCenter> = {}): FactoryCommandCenter => ({
    machines: 2, running: 1, breakdown: 0, idle: 1, maintenance: 0, offline: 0,
    total_downtime_minutes: 0, active_work_orders: 0, behind_plans: 0,
    open_escalations: 0, low_stock_items: 0,
    quality_fail_rate: 1, quality_measured: true, quality_window: "last 7 days",
    zone_summary: [], ...over,
  });

  it("carries its window — it is the only windowed figure in a row of live counts", () => {
    renderTwin(centre());
    expect(screen.getByText("Quality Fail · last 7 days")).toBeTruthy();
    expect(tile("Quality Fail · last 7 days").textContent).toContain("1%");
  });

  it("shows a dash, not 0%, when the window inspected no units", () => {
    renderTwin(centre({ quality_fail_rate: null, quality_measured: false }));
    const box = tile("Quality Fail · last 7 days");
    expect(box.textContent).toContain("—");
    expect(box.textContent).not.toContain("0%");
  });
});

describe("the quality snapshot card", () => {
  it("says the fail rate is not measured instead of printing 0%", async () => {
    apiGet.mockResolvedValue({
      inspections: 1, inspected: 0, passed: 0, failed: 0, rework: 0, scrap: 0,
      first_pass_yield: null, fail_rate: null, measured: false, window: "last 7 days",
      top_defects: [], by_machine: [], by_line: [],
    });
    render(<QualitySnapshot />);
    expect(await screen.findByText(/fail rate not measured/)).toBeTruthy();
    expect(screen.getByText("no units inspected")).toBeTruthy();
    expect(screen.queryByText(/0% fail rate/)).toBeNull();
  });

  it("names the window it pooled", async () => {
    apiGet.mockResolvedValue({
      inspections: 3, inspected: 1000, passed: 990, failed: 10, rework: 0, scrap: 0,
      first_pass_yield: 99, fail_rate: 1, measured: true, window: "last 7 days",
      top_defects: [], by_machine: [], by_line: [],
    });
    render(<QualitySnapshot />);
    expect(await screen.findByText("Quality · last 7 days")).toBeTruthy();
    expect(screen.getByText(/1% fail rate/)).toBeTruthy();
  });
});
