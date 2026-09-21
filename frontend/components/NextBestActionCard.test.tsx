import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * A failed "Raise an escalation" says so, and goes nowhere.
 *
 * The card's catch used to call onRaised(null) — the same call a success makes
 * when the server answers "nothing to raise" — and the dashboard reacts to that
 * by switching to the Escalation Center, refetching and scrolling to the top.
 * A 500 therefore walked the owner to a list to look for a row that was never
 * created, with nothing on screen saying the request had failed. This card
 * never received the dashboard's failed-write banner (#402 lives in page.tsx),
 * so it carries its own.
 */

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
}));

import NextBestActionCard from "./NextBestActionCard";

const SUMMARY = {
  has_data: true, at_world_class: false, unit_value_gbp: null,
  biggest_lever: "availability", lever_label: "Availability", lever_action: "Cut changeover time",
  lever_recoverable_units_per_year: 1200, lever_recoverable_value_per_year: null,
  recoverable_value_per_year: null, recoverable_units_per_year: 3000,
  components: [{ key: "availability", label: "Availability", current: 70, target: 90, gap_points: 20 }],
};

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiGet.mockResolvedValue(SUMMARY);
});

afterEach(cleanup);

describe("NextBestActionCard raising the recovery escalation", () => {
  it("does not route the owner anywhere when the raise failed, and says why in the card", async () => {
    // One-shot, as in UnitRateEditor.test.tsx (a persistent rejection is
    // reported as unhandled by vitest 4 in a multi-test file).
    apiPost.mockRejectedValueOnce(
      new Error(JSON.stringify({ detail: "Escalations are locked while a backup runs" })));
    const onRaised = vi.fn();
    render(<NextBestActionCard onRaised={onRaised} />);
    fireEvent.click(await screen.findByRole("button", { name: /Raise an escalation/ }));

    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain(
      "Could not raise the recovery escalation: Escalations are locked while a backup runs");
    expect(onRaised).not.toHaveBeenCalled();
    // The button is back, so the owner can try again from where they are.
    expect((screen.getByRole("button", { name: /Raise an escalation/ }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("routes with the id when the server raised (or surfaced) the escalation", async () => {
    apiPost.mockResolvedValue({ created: 1, escalation_id: 42 });
    const onRaised = vi.fn();
    render(<NextBestActionCard onRaised={onRaised} />);
    fireEvent.click(await screen.findByRole("button", { name: /Raise an escalation/ }));
    await waitFor(() => expect(onRaised).toHaveBeenCalledWith(42));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("still routes on a null id, which is the server saying there was nothing to raise", async () => {
    // {created: 0, escalation_id: null} is a real answer (the plant is at world
    // class), not a failure; the Escalation Center is the right place to land.
    apiPost.mockResolvedValue({ created: 0, escalation_id: null });
    const onRaised = vi.fn();
    render(<NextBestActionCard onRaised={onRaised} />);
    fireEvent.click(await screen.findByRole("button", { name: /Raise an escalation/ }));
    await waitFor(() => expect(onRaised).toHaveBeenCalledWith(null));
  });

  it("clears a previous failure when the owner tries again", async () => {
    apiPost.mockRejectedValueOnce(new Error("Failed to fetch"));
    apiPost.mockResolvedValueOnce({ created: 1, escalation_id: 7 });
    const onRaised = vi.fn();
    render(<NextBestActionCard onRaised={onRaised} />);
    fireEvent.click(await screen.findByRole("button", { name: /Raise an escalation/ }));
    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toMatch(/no response from the server/);
    fireEvent.click(screen.getByRole("button", { name: /Raise an escalation/ }));
    await waitFor(() => expect(onRaised).toHaveBeenCalledWith(7));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
