import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * "AI Predictive Intelligence" — the surface that suggested things and offered
 * no way to act on any of them.
 *
 * This queue was a dead end. A recommendation's only futures were Acknowledged
 * and Closed: it created no task, entered no approval gate and tracked no
 * outcome, while the AgentAction path beside it did all three (ADR-0005,
 * ADR-0029). Two "AI suggests" surfaces behaving differently is confusing on
 * its own; the weaker one carrying the stronger name is worse.
 *
 * What these tests pin is the restraint as much as the capability. Exactly one
 * kind of recommendation is proposable, because a maintenance task on a machine
 * is the single entry in ev.PROPOSABLE_KINDS. Ordering stock and rebalancing a
 * schedule get a sentence, not a button — a control for them would be a promise
 * AMP cannot keep.
 */

const apiPost = vi.fn();
vi.mock("../lib/api", () => ({
  apiGet: vi.fn(),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  errorDetail: (e: unknown) => (e instanceof Error ? e.message : String(e)),
}));

import AIInsightsSection from "./AIInsightsSection";
import type { AIRecommendation } from "../lib/mega-pack2-types";

const MAINTENANCE: AIRecommendation = {
  id: 1, recommendation_type: "Predictive Maintenance", severity: "High",
  title: "Maintenance risk detected on SMT-Reflow-01",
  message: "SMT-Reflow-01 has 365 minutes downtime in the last 30 days.",
  related_machine_id: 3, confidence: 86, status: "Open",
  action: "Get maintenance to it before it stops.",
  propose: { kind: "maintenance_task", machine_id: 3 },
};
const STOCK: AIRecommendation = {
  id: 2, recommendation_type: "Inventory Forecast", severity: "Medium",
  title: "Inventory replenishment recommended for RM-PASTE-01",
  message: "SAC305 Solder Paste is at 4 kg; reorder level is 12.",
  related_machine_id: null, confidence: 82, status: "Open",
  action: "Order it now — the lead time has to beat what is left on the shelf.",
  propose: null,
};

function renderIt(rows: AIRecommendation[], onOpen?: (v: string) => void) {
  return render(
    <AIInsightsSection
      recommendations={rows}
      insights={null}
      generateRecommendations={() => {}}
      updateRecommendation={() => {}}
      onOpen={onOpen}
    />,
  );
}

beforeEach(() => apiPost.mockReset());
afterEach(cleanup);

describe("AIInsightsSection", () => {
  it("says what to do about every recommendation, not just that it exists", () => {
    renderIt([MAINTENANCE, STOCK]);
    expect(screen.getByText(/Get maintenance to it before it stops/)).toBeTruthy();
    expect(screen.getByText(/the lead time has to beat what is left on the shelf/)).toBeTruthy();
  });

  it("offers a proposal ONLY where AMP can carry it out", () => {
    renderIt([MAINTENANCE, STOCK]);
    // Ordering stock is not AMP's to do; a button for it would be a promise it
    // cannot keep.
    expect(screen.getAllByText("Propose a maintenance task").length).toBe(1);
  });

  it("raises the draft through the one shared write, sending only kind and machine", async () => {
    apiPost.mockResolvedValue({ id: 9, summary: "Open a High maintenance task for SMT-Reflow-01" });
    renderIt([MAINTENANCE]);
    fireEvent.click(screen.getByText("Propose a maintenance task"));
    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    const [path, body] = apiPost.mock.calls[0];
    expect(path).toBe("/agent-actions/propose");
    expect(body).toEqual({ kind: "maintenance_task", machine_id: 3 });
  });

  it("nothing is carried out by raising it", async () => {
    apiPost.mockResolvedValue({ id: 9, summary: "x" });
    renderIt([MAINTENANCE]);
    fireEvent.click(screen.getByText("Propose a maintenance task"));
    expect((await screen.findByRole("status")).textContent).toContain("Nothing has been carried out yet");
  });

  it("renders a recommendation with no action at all, rather than breaking", () => {
    // A row from a server that predates the computed fields, or a kind nobody
    // has written advice for yet.
    const bare = { ...STOCK, id: 3, action: null, propose: null };
    renderIt([bare]);
    expect(screen.getByText(bare.title)).toBeTruthy();
    expect(screen.queryByText("Propose a maintenance task")).toBeNull();
  });

  it("still lets a person close one off, which was all it could ever do before", () => {
    const update = vi.fn();
    render(
      <AIInsightsSection
        recommendations={[MAINTENANCE]}
        insights={null}
        generateRecommendations={() => {}}
        updateRecommendation={update}
      />,
    );
    fireEvent.change(screen.getByDisplayValue("Open"), { target: { value: "Closed" } });
    expect(update).toHaveBeenCalledWith(1, "Closed");
  });
});
