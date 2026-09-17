import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * A refused decision must refresh the queue.
 *
 * When the item behind a proposal was changed or deleted, the backend refuses
 * the decision (409) and WITHDRAWS the proposal (ADR-0015 addendum). An inbox
 * that only showed the error would keep offering Approve / Reject on a proposal
 * that no longer exists, and every further click would bounce. So the inbox
 * reloads after a failure, and still shows the backend's reason.
 */

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
  apiPatch: vi.fn(),
}));

import ApprovalsInbox from "./ApprovalsInbox";

const PROPOSAL = {
  id: 5, agent: "maintenance", action_type: "open_task", summary: "Open a Critical task",
  ref_kind: "maintenance_task", ref_id: 3, severity: "Critical", status: "Proposed",
  related_machine_id: null, created_at: "2026-09-17T08:00:00",
};

afterEach(cleanup);

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
});

describe("ApprovalsInbox after a refused decision", () => {
  it("reloads the queue so a withdrawn proposal disappears, and keeps the reason", async () => {
    let queue = [PROPOSAL];
    apiGet.mockImplementation((p: string) =>
      Promise.resolve(p.startsWith("/agent-actions") ? queue : []));
    apiPost.mockImplementation(() => {
      queue = [];   // the server withdrew it while refusing
      return Promise.reject(new Error("Nothing was decided: the maintenance task it would change no longer exists. It has been withdrawn."));
    });

    render(<ApprovalsInbox />);
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve" })).toBeNull());
    expect(screen.getByText(/It has been withdrawn\./)).toBeTruthy();
    expect(apiGet.mock.calls.filter(([p]) => String(p).startsWith("/agent-actions")).length).toBe(2);
  });
});
