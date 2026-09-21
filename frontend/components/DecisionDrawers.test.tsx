import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The agent drawer and the machine cockpit drawer decide proposals too.
 *
 * Round-2 verification fixed the Approvals inbox and Mission Control: an expired
 * proposal can only be rejected, and a refused decision (409: the server withdrew
 * the proposal, or someone decided it first) reloads the list. These two drawers
 * kept the old behaviour: Approve on an expired proposal, and after a refusal the
 * same Approve / Reject stayed on screen for a proposal that no longer existed.
 */

const apiGet = vi.fn();
const apiPost = vi.fn();
// The signed-in role: the machine cockpit offers Approve / Reject only to a
// role the server lets decide (Admin or Supervisor).
let role = "Admin";

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
  getUserRole: () => role,
}));

import AgentDetailDrawer from "./AgentDetailDrawer";
import MachineDetailDrawer from "./MachineDetailDrawer";
import { DECISION_ROLE_NOTE, EXPIRED_PROPOSAL_NOTE } from "../lib/agent-actions";

function action(id: number, expired: boolean) {
  return {
    id, action_type: "open_task", summary: `proposal ${id}`, ref_kind: "maintenance_task",
    ref_id: 10 + id, severity: "High", status: "Proposed", related_machine_id: 1,
    created_at: "2026-09-10T08:00:00", decided_by: null, decided_at: null, expired,
    agent: "maintenance",
  };
}

const AGENT = {
  key: "maintenance", name: "Maintenance agent", watches: "risk", acts: "tasks",
  auto_approves: false, total_actions: 2, pending: 2, approved: 0, rejected: 0,
  approval_rate: null, outputs: {}, last_action_at: null, daily: [],
  recent: [action(1, false), action(2, true)],
};

const MACHINE = {
  machine_id: 1, name: "PRESS-01", line: "SMT", status: "Running", utilization: 80,
  downtime: "0 min", health_score: 80, health_band: "Good", risk_score: 20, risk_level: "Low",
  oee: { oee: 80, availability: 90, performance: 90, quality: 99, has_data: true },
  risk_factors: [], downtime_7d: [],
  production_7d: { good: 0, total: 0, good_rate: 0, daily: [] },
  quality: { inspections: 0, inspected: 0, passed: 0, failed: 0, fail_rate: 0, top_defects: [] },
  open_actions: [action(1, false), action(2, true)],
  timeline: [],
};

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
});

function approveButtons() {
  return screen.getAllByRole("button", { name: "Approve" }) as HTMLButtonElement[];
}

describe.each([
  ["AgentDetailDrawer", AGENT, (onChanged: () => void) =>
    <AgentDetailDrawer agentKey="maintenance" onClose={() => {}} onChanged={onChanged} />],
  ["MachineDetailDrawer", MACHINE, (onChanged: () => void) =>
    <MachineDetailDrawer machineId={1} onClose={() => {}} onChanged={onChanged} />],
] as const)("%s", (_name, payload, mount) => {
  it("offers only Reject on an expired proposal, and says why", async () => {
    apiGet.mockResolvedValue(payload);
    render(mount(() => {}));
    await screen.findByText("proposal 2");
    const [fresh, stale] = approveButtons();
    expect(fresh.disabled).toBe(false);
    expect(stale.disabled).toBe(true);
    expect(screen.getByText(EXPIRED_PROPOSAL_NOTE)).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Reject" }).every((b) => !(b as HTMLButtonElement).disabled)).toBe(true);
  });

  it("reloads after a refused decision instead of keeping the buttons", async () => {
    apiGet.mockResolvedValue(payload);
    apiPost.mockRejectedValue(new Error("This proposal was withdrawn: the task it would change no longer exists."));
    const onChanged = vi.fn();
    render(mount(onChanged));
    await screen.findByText("proposal 1");
    const loadsBefore = apiGet.mock.calls.length;
    fireEvent.click(approveButtons()[0]);
    await screen.findByText(/was withdrawn/);
    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThan(loadsBefore));
    expect(onChanged).toHaveBeenCalled();
  });
});

describe("MachineDetailDrawer offers a decision only to a role the server lets decide", () => {
  // The cockpit opens from the OEE, production, quality and downtime snapshots
  // and the machine-health list — all Operator-visible — and used to offer an
  // Operator Approve / Reject on every open proposal; the click came back 403.
  it("shows an Operator the open proposals with no buttons, and says whose decision it is", async () => {
    role = "Operator";
    apiGet.mockResolvedValue(MACHINE);
    render(<MachineDetailDrawer machineId={1} onClose={() => {}} onChanged={() => {}} />);
    await screen.findByText("proposal 1");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    // One note per proposal saying whose decision it is (plus the expiry note on
    // the expired one).
    expect(screen.getAllByText(DECISION_ROLE_NOTE)).toHaveLength(2);
    role = "Admin";
  });
});
