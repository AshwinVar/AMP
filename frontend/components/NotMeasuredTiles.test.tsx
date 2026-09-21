import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * A tile shows a dash when the backend measured nothing — never 0.
 *
 * Six figures published `round(part / whole * 100) if whole else 0`, and which
 * lie that told depended on the scale:
 *
 *   "Achievement 0%"     no work order carries a target -> a total failure to deliver
 *   "Quality 0%"         no job logged a unit           -> every part was scrap
 *   "Avg Repair 0m"      no task completed              -> a perfect repair record
 *   "Autonomy 0%"        no decision made               -> every one needed a human
 *   "Avg Utilization 0%" no machine reported            -> an idle plant
 *
 * The backend now sends null with a `*_measured` flag. These are the screens
 * that have to render that rather than `?? 0`.
 *
 * The Autonomy tile had a second defect: it showed the LIFETIME auto-approval
 * rate under a caption reading "N actions / 7d" — the only window named on the
 * tile, attached to the denominator of an all-time rate.
 */

const apiGet = vi.fn();
vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

import FactoryPulse from "./FactoryPulse";
import MaintenanceSection from "./MaintenanceSection";
import OperatorTerminalSection from "./OperatorTerminalSection";
import ProductionPlanSection from "./ProductionPlanSection";
import WorkOrdersSection from "./WorkOrdersSection";

beforeEach(() => apiGet.mockReset());
afterEach(cleanup);

/** A KPI tile renders its title and value inside one box. */
const tile = (title: string) => screen.getByText(title).parentElement as HTMLElement;

const noop = () => {};
const MACHINES = [{ id: 1, name: "SMT-Reflow-01", status: "Running", utilization: 82, downtime: "0 min" }];
// Every one of these sections renders a create form off `form`, so the
// fields it reads have to exist even though this suite only asserts tiles.
const FORM = new Proxy({}, { get: () => "" }) as never;

describe("Achievement tiles", () => {
  it("work orders: a dash when no order carries a target", () => {
    render(
      <WorkOrdersSection
        machines={MACHINES}
        workOrders={[]}
        analytics={{
          total_work_orders: 1, planned: 1, running: 0, completed: 0, delayed: 0,
          total_target: 0, total_actual: 0, achievement: null, achievement_measured: false,
        } as never}
        form={FORM}
        setForm={noop}
        createWorkOrder={noop}
        updateWorkOrder={noop}
        getMachineName={() => "SMT-Reflow-01"}
      />,
    );
    expect(tile("Achievement").textContent).toContain("—");
    expect(tile("Achievement").textContent).not.toContain("0%");
    // The COUNT beside it is a real zero and must still read as one. Scoped to
    // the KPI row: "Target" is also a field label on the create form below.
    const kpi = screen.getAllByText("Target")[0].parentElement as HTMLElement;
    expect(kpi.textContent).toContain("0");
  });

  it("work orders: the real rate when a target exists", () => {
    render(
      <WorkOrdersSection
        machines={MACHINES}
        workOrders={[]}
        analytics={{
          total_work_orders: 1, planned: 0, running: 0, completed: 1, delayed: 0,
          total_target: 100, total_actual: 90, achievement: 90, achievement_measured: true,
        } as never}
        form={FORM}
        setForm={noop}
        createWorkOrder={noop}
        updateWorkOrder={noop}
        getMachineName={() => "SMT-Reflow-01"}
      />,
    );
    expect(tile("Achievement").textContent).toContain("90%");
  });

  it("production plans: a dash when nothing was planned", () => {
    render(
      <ProductionPlanSection
        machines={MACHINES}
        workOrders={[]}
        plans={[]}
        analytics={{
          total_plans: 1, planned_quantity: 0, actual_quantity: 0,
          achievement: null, achievement_measured: false,
          planned: 1, running: 0, completed: 0, delayed: 0,
        } as never}
        form={FORM}
        setForm={noop}
        createPlan={noop}
        updatePlan={noop}
        getMachineName={() => "SMT-Reflow-01"}
      />,
    );
    expect(tile("Achievement").textContent).toContain("—");
    expect(tile("Achievement").textContent).not.toContain("0%");
  });
});

describe("Maintenance and operator tiles", () => {
  it("average repair is a dash, not 0m, with no completed task", () => {
    render(
      <MaintenanceSection
        machines={MACHINES}
        tasks={[]}
        analytics={{
          total_tasks: 1, open: 1, in_progress: 0, completed: 0, proposed: 0,
          cancelled: 0, other: 0, overdue: 0, preventive: 1, breakdown: 0,
          total_downtime_minutes: 0, avg_repair_minutes: null, avg_repair_measured: false,
          machine_counts: {},
        } as never}
        form={FORM}
        setForm={noop}
        createTask={noop}
        updateTask={noop}
        generateOverdueEscalations={noop}
        getMachineName={() => "SMT-Reflow-01"}
      />,
    );
    expect(tile("Avg Repair").textContent).toContain("—");
    expect(tile("Avg Repair").textContent).not.toContain("0m");
  });

  it("operator quality is a dash, not 0%, when no unit was logged", () => {
    render(
      <OperatorTerminalSection
        machines={MACHINES}
        workOrders={[]}
        productionPlans={[]}
        executions={[]}
        analytics={{
          total_jobs: 1, started: 1, paused: 0, completed: 0, other: 0,
          good_count: 0, rejected_count: 0, quality_rate: null, quality_measured: false,
        } as never}
        form={FORM}
        setForm={noop}
        createExecution={noop}
        updateExecution={noop}
        getMachineName={() => "SMT-Reflow-01"}
      />,
    );
    expect(tile("Quality").textContent).toContain("—");
    expect(tile("Quality").textContent).not.toContain("0%");
    expect(tile("Good").textContent).toContain("0");
  });
});

describe("the command header's Autonomy tile", () => {
  function pulse(agents: Record<string, unknown>) {
    return {
      fleet: { machines: 1, measured: 1, avg_health: 71, needs_attention: 0, worst: null },
      agents: {
        agents_active: 1, actions_7d: 3, auto_rate: 50, auto_measured: true,
        auto_decided: 2, auto_window: "last 7 days", awaiting_you: 0, ...agents,
      },
      headline: "Fleet health 71 · all clear",
    };
  }

  it("names the window its rate covers, once", async () => {
    apiGet.mockResolvedValue(pulse({}));
    render(<FactoryPulse />);
    await screen.findByText("Autonomy");
    const box = tile("Autonomy");
    expect(box.textContent).toContain("50%");
    expect(box.textContent).toContain("3 actions / last 7 days");
  });

  it("shows a dash when the week decided nothing", async () => {
    apiGet.mockResolvedValue(pulse({ auto_rate: null, auto_measured: false, auto_decided: 0 }));
    render(<FactoryPulse />);
    await screen.findByText("Autonomy");
    const box = tile("Autonomy");
    expect(box.textContent).toContain("—");
    expect(box.textContent).not.toContain("0%");
    expect(box.textContent).toContain("no decisions this week");
  });
});
