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

describe("ApprovalsInbox reaches every proposal", () => {
  // Measured before paging: one held Draft PO whose proposal was older than 300
  // newer ones. The inbox asked once, got 300 rows, and the held PO could be
  // neither edited (409 "decide it in Approvals") nor found in Approvals.
  it("offers older proposals past the first page, including the one holding an item", async () => {
    const newer = Array.from({ length: 300 }, (_, i) => ({ ...PROPOSAL, id: 1000 - i, summary: `newer ${i}` }));
    const held = { ...PROPOSAL, id: 7, summary: "Draft a PO for steel (held)" };
    apiGet.mockImplementation((p: string) => {
      if (!p.startsWith("/agent-actions")) return Promise.resolve([]);
      const offset = Number(new URLSearchParams(p.split("?")[1]).get("offset"));
      return Promise.resolve([...newer, held].slice(offset, offset + 300));
    });

    // 300 cards render twice; under a parallel full run that is slow, not wrong.
    const wait = { timeout: 20000 };
    render(<ApprovalsInbox />);
    const older = await screen.findByRole("button", { name: "Load older proposals" }, wait);
    expect(screen.queryByText("Draft a PO for steel (held)")).toBeNull();
    expect(screen.getByText("300+")).toBeTruthy();

    fireEvent.click(older);
    expect(await screen.findByText("Draft a PO for steel (held)", {}, wait)).toBeTruthy();
    expect(screen.getByText("301")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Load older proposals" })).toBeNull();
  }, 60000);

  it("says an expired proposal can only be rejected, and does not offer Approve on it", async () => {
    apiGet.mockImplementation((p: string) =>
      Promise.resolve(p.startsWith("/agent-actions") ? [{ ...PROPOSAL, expired: true }] : []));

    render(<ApprovalsInbox />);
    const approve = await screen.findByRole("button", { name: "Approve" });
    expect((approve as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Reject" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole("note").textContent).toMatch(/can only be rejected, which releases the item/);
    fireEvent.click(approve);
    expect(apiPost).not.toHaveBeenCalled();
  });
});
