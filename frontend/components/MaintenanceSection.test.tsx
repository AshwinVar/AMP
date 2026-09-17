import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import MaintenanceSection from "./MaintenanceSection";
import type { MaintenanceTask } from "../lib/mega-pack1-types";

/**
 * An agent-proposed task is held until an Admin or Supervisor decides it.
 *
 * The backend refuses every PATCH and DELETE on a held task (409) and marks it
 * with `awaiting_approval`. The screen must say so and offer nothing that would
 * only bounce -- and it must decide that from the flag alone: a task a human
 * created with status "Proposed" has no proposal behind it and keeps every
 * control. Both rows below carry status "Proposed"; only the flag differs.
 */

afterEach(cleanup);

const noop = () => undefined;

function task(over: Partial<MaintenanceTask> = {}): MaintenanceTask {
  return {
    id: 1,
    task_no: "MT-1",
    machine_id: 1,
    task_type: "Predictive (auto)",
    priority: "Critical",
    assigned_to: "Maintenance team",
    planned_date: "2026-09-17",
    completed_date: null,
    downtime_minutes: 0,
    status: "Proposed",
    notes: null,
    ...over,
  };
}

function renderWith(tasks: MaintenanceTask[], canDelete = true) {
  return render(
    <MaintenanceSection
      machines={[]}
      tasks={tasks}
      analytics={null}
      form={{ task_no: "", machine_id: "", task_type: "Preventive", priority: "Medium", assigned_to: "", planned_date: "", status: "Open" }}
      setForm={noop}
      createTask={noop}
      updateTask={noop}
      deleteTask={canDelete ? noop : undefined}
      generateOverdueEscalations={noop}
      getMachineName={() => "CNC-01"}
    />,
  );
}

function rowOf(taskNo: string) {
  return screen.getByText(taskNo).closest("tr") as HTMLElement;
}

describe("MaintenanceSection held rows", () => {
  it("locks a held task and explains who decides it; leaves a look-alike editable", () => {
    renderWith([
      task({ id: 1, task_no: "AUTO-MAINT-1", awaiting_approval: { agent_action_id: 42, agent: "maintenance", expired: false } }),
      task({ id: 2, task_no: "MT-HUMAN", awaiting_approval: null }),
    ]);

    const held = rowOf("AUTO-MAINT-1");
    expect((within(held).getByLabelText("Status of AUTO-MAINT-1") as HTMLSelectElement).disabled).toBe(true);
    expect((within(held).getByRole("spinbutton") as HTMLInputElement).disabled).toBe(true);
    expect(within(held).queryByRole("button", { name: "Delete" })).toBeNull();
    expect(within(held).getByRole("note").textContent).toBe(
      "Awaiting approval: proposed by the maintenance agent (action #42). An Admin or Supervisor approves or rejects it in Approvals.",
    );

    const lookalike = rowOf("MT-HUMAN");
    expect((within(lookalike).getByLabelText("Status of MT-HUMAN") as HTMLSelectElement).disabled).toBe(false);
    expect((within(lookalike).getByRole("spinbutton") as HTMLInputElement).disabled).toBe(false);
    expect(within(lookalike).getByRole("button", { name: "Delete" })).toBeTruthy();
    expect(within(lookalike).queryByRole("note")).toBeNull();
  });

  it("says an expired proposal must be rejected", () => {
    renderWith([task({ task_no: "AUTO-MAINT-OLD", awaiting_approval: { agent_action_id: 7, agent: "yield", expired: true } })]);
    expect(within(rowOf("AUTO-MAINT-OLD")).getByRole("note").textContent).toBe(
      "This proposal has expired; an Admin or Supervisor must reject it in Approvals before it can be changed.",
    );
  });

  it("renders no Delete at all when the viewer has no delete handler", () => {
    renderWith([task({ task_no: "MT-OPEN", status: "Open" })], false);
    expect(within(rowOf("MT-OPEN")).queryByRole("button", { name: "Delete" })).toBeNull();
  });
});
