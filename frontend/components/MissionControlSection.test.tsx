import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Mission Control is a third place an approver decides agent proposals
 * (Approve / Reject on each "action" in the feed), so it owes what the Approvals
 * inbox and the activity log owe (ADR-0015 addendum). Measured before this
 * (verifier round 2): a refused decision only showed the error, so a proposal
 * the server had just withdrawn (409 "It has been withdrawn") kept its buttons
 * until the 30-second poll and a second click got 400 "Already cancelled"; and
 * Approve was offered on expired proposals, which the server refuses.
 */

const apiGet = vi.fn();
const apiPost = vi.fn();
// The signed-in role. Approve / Reject are offered only to a role
// POST /agent-actions/{id}/approve|reject lets decide (Admin or Supervisor).
let role = "Admin";

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
  apiPatch: vi.fn(),
  getUserRole: () => role,
}));
vi.mock("./FactoryPulse", () => ({ default: () => null }));

import MissionControlSection from "./MissionControlSection";
import { DECISION_ROLE_NOTE } from "../lib/agent-actions";

const ACTION = {
  source: "action", kind: "open_task", severity: "Critical",
  title: "Open a Critical maintenance task for CNC-9",
  message: "Maintenance agent · proposed, awaiting approval.",
  occurred_at: "2026-09-17T08:00:00", related_machine_id: 9, ref_id: 5, expired: false,
};

afterEach(cleanup);

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  role = "Admin";
});

describe("MissionControlSection offers a decision only to a role the server lets decide", () => {
  it("shows an Operator no Approve / Reject on a proposal, and says whose decision it is", async () => {
    // Mission Control is an Operator-visible view (lib/modules OPERATOR_VIEWS);
    // the buttons used to be offered and the click came back 403.
    role = "Operator";
    apiGet.mockImplementation((p: string) => Promise.resolve(p === "/insights" ? [ACTION] : null));

    render(<MissionControlSection />);
    await screen.findByText(ACTION.title);
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(screen.getByRole("note").textContent).toBe(DECISION_ROLE_NOTE);
  });

  it("offers a Supervisor both buttons, with no note", async () => {
    role = "Supervisor";
    apiGet.mockImplementation((p: string) => Promise.resolve(p === "/insights" ? [ACTION] : null));

    render(<MissionControlSection />);
    await screen.findByRole("button", { name: "Approve" });
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
    expect(screen.queryByRole("note")).toBeNull();
  });
});

describe("MissionControlSection agent proposals", () => {
  it("reloads the feed after a refused decision, so a withdrawn proposal disappears, and keeps the reason", async () => {
    let feed = [ACTION];
    apiGet.mockImplementation((p: string) => Promise.resolve(p === "/insights" ? feed : null));
    apiPost.mockImplementation(() => {
      feed = [];   // the server withdrew it while refusing
      return Promise.reject(new Error(
        "Nothing was decided: the maintenance task it would change no longer exists, so this proposal can no longer take effect. It has been withdrawn."));
    });

    render(<MissionControlSection />);
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve" })).toBeNull());
    expect(screen.getByText(/It has been withdrawn\./)).toBeTruthy();
    expect(apiGet.mock.calls.filter(([p]) => p === "/insights").length).toBe(2);
  });

  it("says an expired proposal can only be rejected, and does not offer Approve on it", async () => {
    apiGet.mockImplementation((p: string) =>
      Promise.resolve(p === "/insights" ? [{ ...ACTION, expired: true }] : null));

    render(<MissionControlSection />);
    const approve = await screen.findByRole("button", { name: "Approve" });
    expect((approve as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Reject" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole("note").textContent).toMatch(/can only be rejected, which releases the item/);
    fireEvent.click(approve);
    expect(apiPost).not.toHaveBeenCalled();
  });

  it("offers Approve on a proposal that has not expired, with no note", async () => {
    apiGet.mockImplementation((p: string) => Promise.resolve(p === "/insights" ? [ACTION] : null));

    render(<MissionControlSection />);
    const approve = await screen.findByRole("button", { name: "Approve" });
    expect((approve as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByRole("note")).toBeNull();
  });
});
