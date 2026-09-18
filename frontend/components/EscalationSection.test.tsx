import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import EscalationSection from "./EscalationSection";
import type { Escalation } from "../lib/phase12-types";

/**
 * An agent-proposed escalation is held until an Admin or Supervisor decides it.
 * Both rows below are status "Proposed"; only `awaiting_approval` differs, and
 * only it may decide whether the owner, department, status and resolution
 * controls and the Delete button are offered.
 */

afterEach(cleanup);

const noop = () => undefined;

function escalation(over: Partial<Escalation> = {}): Escalation {
  return {
    id: 1,
    machine_id: null,
    title: "Repeated downtime",
    severity: "High",
    owner: "Maintenance Lead",
    department: "Maintenance",
    status: "Proposed",
    source: "Escalation agent",
    notes: null,
    resolution_notes: null,
    ...over,
  };
}

function renderWith(rows: Escalation[], canDelete = true) {
  return render(
    <EscalationSection
      machines={[]}
      escalations={rows}
      analytics={null}
      form={{ machine_id: "", title: "", severity: "High", owner: "", department: "", status: "Open", source: "Manual", notes: "" }}
      setForm={noop}
      createEscalation={noop}
      updateEscalation={noop}
      deleteEscalation={canDelete ? noop : undefined}
      generateFromSmartAlerts={noop}
      getMachineName={() => "CNC-01"}
    />,
  );
}

function rowOf(title: string) {
  return screen.getByText(title).closest("tr") as HTMLElement;
}

describe("EscalationSection held rows", () => {
  it("locks a held escalation's controls and hides Delete; a look-alike keeps them", () => {
    renderWith([
      escalation({ id: 1, title: "Agent escalation", awaiting_approval: { agent_action_id: 9, agent: "escalation", expired: false } }),
      escalation({ id: 2, title: "Human escalation", source: "Manual", awaiting_approval: null }),
    ]);

    const held = rowOf("Agent escalation");
    for (const label of ["Owner", "Department", "Status", "Resolution notes"]) {
      expect((within(held).getByLabelText(`${label} of Agent escalation`) as HTMLInputElement).disabled).toBe(true);
    }
    expect(within(held).queryByRole("button", { name: "Delete" })).toBeNull();
    expect(within(held).getByRole("note").textContent).toBe(
      "Awaiting approval: proposed by the escalation agent (action #9). An Admin or Supervisor approves or rejects it in Approvals.",
    );

    const lookalike = rowOf("Human escalation");
    for (const label of ["Owner", "Department", "Status", "Resolution notes"]) {
      expect((within(lookalike).getByLabelText(`${label} of Human escalation`) as HTMLInputElement).disabled).toBe(false);
    }
    expect(within(lookalike).getByRole("button", { name: "Delete" })).toBeTruthy();
    expect(within(lookalike).queryByRole("note")).toBeNull();
  });

  it("renders no Delete when the viewer has no delete handler", () => {
    renderWith([escalation({ title: "Open one", status: "Open" })], false);
    expect(within(rowOf("Open one")).queryByRole("button", { name: "Delete" })).toBeNull();
  });
});
