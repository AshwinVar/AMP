import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The agent activity log decides proposals too (Approve / Reject on Proposed
 * rows), so it shares the Approvals inbox's two obligations (ADR-0015
 * addendum): every proposal must be reachable past the server's 300-row page,
 * and an expired one must say it can only be rejected instead of offering an
 * Approve the server will refuse.
 */

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
}));
vi.mock("./ActionOutcomesSection", () => ({ default: () => null }));
vi.mock("./AgentPolicyPanel", () => ({ default: () => null }));
vi.mock("./AgentDetailDrawer", () => ({ default: () => null }));
// The AMP-native AI panels this section also mounts (ADR-0020) have their own
// suites (AILearningConsentCard.test.tsx, AIModelCard.test.tsx); stubbed here like
// the other child panels so this suite tests the agent activity section alone.
vi.mock("./AILearningConsentCard", () => ({ default: () => null }));
vi.mock("./AIModelCard", () => ({ default: () => null }));

import AgentActivitySection from "./AgentActivitySection";

const ROW = {
  id: 5, agent: "reorder", action_type: "draft_po", summary: "Draft a PO for steel",
  ref_kind: "purchase_order", ref_id: 3, severity: "Medium", status: "Proposed",
  related_machine_id: null, created_at: "2026-09-17T08:00:00", decided_by: null,
  decided_at: null, expired: false,
};

function serve(rows: (typeof ROW)[]) {
  apiGet.mockImplementation((p: string) => {
    if (p.startsWith("/agent-actions?")) {
      const offset = Number(new URLSearchParams(p.split("?")[1]).get("offset"));
      return Promise.resolve(rows.slice(offset, offset + 300));
    }
    if (p === "/agent-roster") return Promise.resolve([]);
    return Promise.resolve(null);
  });
}

afterEach(cleanup);

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
});

describe("AgentActivitySection", () => {
  it("does not offer Approve on an expired proposal, and says why", async () => {
    serve([{ ...ROW, expired: true }]);
    render(<AgentActivitySection />);
    const approve = await screen.findByRole("button", { name: "Approve" });
    expect((approve as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Reject" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole("note").textContent).toMatch(/can only be rejected, which releases the item/);
  });

  it("loads older activity past the first page", async () => {
    const newer = Array.from({ length: 300 }, (_, i) => ({ ...ROW, id: 1000 - i, summary: `newer ${i}` }));
    serve([...newer, { ...ROW, id: 7, summary: "the oldest proposal" }]);
    const wait = { timeout: 20000 };
    render(<AgentActivitySection />);
    const older = await screen.findByRole("button", { name: "Load older activity" }, wait);
    expect(screen.queryByText("the oldest proposal")).toBeNull();
    fireEvent.click(older);
    expect(await screen.findByText("the oldest proposal", {}, wait)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Load older activity" })).toBeNull();
  }, 60000);
});
