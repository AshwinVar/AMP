import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The card that raises an action the Copilot drafted (ADR-0039).
 *
 * The whole point of the draft is that NOTHING HAS HAPPENED when it renders,
 * and that raising it still is not the thing taking effect — a person has to
 * approve it afterwards. A card that reads like a confirmation is exactly how
 * somebody comes to believe a job was booked that was not, so these tests are
 * mostly about what the copy says and when.
 *
 * The other half is the payload: only `kind` and `machine_id` may be sent. The
 * server re-derives everything else from the machine, and if this card ever
 * started posting a priority it would look like it worked while the backend
 * quietly ignored it (test_copilot_actions.py section 8 pins the other side).
 */

const apiPost = vi.fn();
vi.mock("../lib/api", () => ({
  apiGet: vi.fn(),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  errorDetail: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}));

import CopilotProposal from "./CopilotProposal";

const DRAFT = {
  kind: "maintenance_task" as const,
  machine_id: 7,
  machine: "CNC-01",
  priority: "High",
  task_type: "Predictive (Copilot)",
  summary: "Open a High maintenance task for CNC-01",
  reason: "CNC-01 is at risk 62 out of 100 (3 breakdowns in the window).",
  label: "Propose a High maintenance task for CNC-01",
};

beforeEach(() => apiPost.mockReset());
afterEach(cleanup);

describe("CopilotProposal", () => {
  it("renders nothing when the answer drafted no action", () => {
    const { container } = render(<CopilotProposal proposal={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("says nothing exists yet, before anything is pressed", () => {
    render(<CopilotProposal proposal={DRAFT} />);
    expect(screen.getByText("AMP would propose")).toBeTruthy();
    expect(screen.getByText(DRAFT.label)).toBeTruthy();
    expect(screen.getByText(/Nothing exists yet/)).toBeTruthy();
    expect(screen.getByText(/only takes effect once somebody approves it/)).toBeTruthy();
  });

  it("shows the priority AMP derived, not one the user chose", () => {
    render(<CopilotProposal proposal={DRAFT} />);
    expect(screen.getByText("High")).toBeTruthy();
    expect(screen.getByText(DRAFT.reason)).toBeTruthy();
  });

  it("sends ONLY the kind and the machine id", async () => {
    apiPost.mockResolvedValue({ id: 12, summary: DRAFT.summary });
    render(<CopilotProposal proposal={DRAFT} />);
    fireEvent.click(screen.getByText("Propose this action"));
    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    const [path, body] = apiPost.mock.calls[0];
    expect(path).toBe("/agent-actions/propose");
    expect(body).toEqual({ kind: "maintenance_task", machine_id: 7 });
    // Not the priority, not the wording, not the task type.
    expect(Object.keys(body as object).sort()).toEqual(["kind", "machine_id"]);
  });

  it("after raising, says it is waiting for approval and NOT carried out", async () => {
    apiPost.mockResolvedValue({ id: 12, summary: DRAFT.summary });
    render(<CopilotProposal proposal={DRAFT} />);
    fireEvent.click(screen.getByText("Propose this action"));
    expect(await screen.findByRole("status")).toBeTruthy();
    expect(screen.getByText(/Proposed — waiting for approval/)).toBeTruthy();
    expect(screen.getByText(/Nothing has been carried out yet/)).toBeTruthy();
    // The button is gone, so the same draft cannot be raised twice from one card.
    expect(screen.queryByText("Propose this action")).toBeNull();
  });

  it("offers a way to the approval queue once raised", async () => {
    apiPost.mockResolvedValue({ id: 12, summary: DRAFT.summary });
    const onOpen = vi.fn();
    render(<CopilotProposal proposal={DRAFT} onOpen={onOpen} />);
    fireEvent.click(screen.getByText("Propose this action"));
    fireEvent.click(await screen.findByText("Open Approvals →"));
    expect(onOpen).toHaveBeenCalledWith("inbox");
  });

  it("shows the server's own refusal, not a generic failure", async () => {
    apiPost.mockRejectedValueOnce(
      new Error('{"detail":"CNC-01 already has an open maintenance task raised from the Copilot."}'),
    );
    render(<CopilotProposal proposal={DRAFT} />);
    fireEvent.click(screen.getByText("Propose this action"));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("already has an open maintenance task");
    // A refusal is not a success: the card must not claim it was proposed.
    expect(screen.queryByText(/waiting for approval/)).toBeNull();
    expect(screen.getByText("Propose this action")).toBeTruthy();
  });
});
