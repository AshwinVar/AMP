import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import ProactiveSection, { byReason } from "./ProactiveSection";

/**
 * The restraint is the feature (ADR-0031).
 *
 * What is pinned: the bar is printed on the card, the held-back items are
 * visible with the reason each was held back, opening the page notifies nobody,
 * and only an Admin or Supervisor can send.
 */
vi.mock("../lib/api", () => ({ apiGet: vi.fn(), apiPost: vi.fn(), getUserRole: vi.fn() }));
const { apiGet, apiPost, getUserRole } = await import("../lib/api");

const BAR =
  "AMP interrupts for three things only: a machine stopped right now, a risk the radar calls LIKELY, and an approved action whose metric came back worse. Everything else is on the dashboard.";

const plan = (over: Record<string, unknown> = {}) => ({
  state: "OK",
  headline: "2 things worth telling you now; 3 held back.",
  considered: 5,
  qualified: [
    { signature: "problem:machines_down", kind: "Machine", severity: "Critical",
      title: "1 machine down right now", message: "CNC-01",
      why: "a machine is stopped right now", view: "machines" },
    { signature: "risk:stock.RM-STEEL", kind: "Risk", severity: "Warning",
      title: "Steel bar is already out of stock", message: "nothing on hand (rule: there is no stock on hand)",
      why: "the radar calls this LIKELY", view: "inventory" },
  ],
  suppressed: [
    { signature: "risk:order.ORD-9", kind: "Risk", severity: "Info",
      title: "ORD-9 may miss its date", message: "within 30% of the measured rate",
      why: "the radar calls this POSSIBLE, which does not interrupt", view: "orders",
      suppressed_by: "BELOW THE BAR" },
    { signature: "problem:downtime.seal", kind: "Problem", severity: "Warning",
      title: "Hydraulic seal leak stopped production 5 times", message: "225 minutes lost",
      why: "ranked on the card, but not happening this second", view: "downtime",
      suppressed_by: "BELOW THE BAR" },
    { signature: "risk:machine.CNC-01", kind: "Risk", severity: "Warning",
      title: "CNC-01 is likely to stop", message: "rule score 80",
      why: "AMP said this within the last 24 hours, at 2026-09-20T09:00:00", view: "machines",
      suppressed_by: "SAID RECENTLY" },
  ],
  cooldown_hours: 24,
  max_per_run: 5,
  bar: BAR,
  ...over,
});

describe("ProactiveSection", () => {
  beforeEach(() => {
    vi.mocked(apiGet).mockReset();
    vi.mocked(apiPost).mockReset();
    vi.mocked(getUserRole).mockReturnValue("Admin");
    vi.mocked(apiGet).mockResolvedValue(plan());
  });

  it("prints the bar, so the threshold can be argued with", async () => {
    render(<ProactiveSection />);
    expect(await screen.findByText(BAR)).toBeTruthy();
  });

  it("says why each thing it would raise clears the bar", async () => {
    render(<ProactiveSection />);
    expect(await screen.findByText("1 machine down right now")).toBeTruthy();
    expect(screen.getByText("Raised because a machine is stopped right now.")).toBeTruthy();
    expect(screen.getByText("Raised because the radar calls this LIKELY.")).toBeTruthy();
  });

  it("shows what it is holding back, grouped by the reason", async () => {
    render(<ProactiveSection />);
    const toggle = await screen.findByRole("button", { name: /Show the 3 AMP is holding back/ });
    fireEvent.click(toggle);
    expect(screen.getByText("BELOW THE BAR (2)")).toBeTruthy();
    expect(screen.getByText("SAID RECENTLY (1)")).toBeTruthy();
    expect(screen.getByText(/the radar calls this POSSIBLE, which does not interrupt/)).toBeTruthy();
    expect(screen.getByText(/AMP said this within the last 24 hours/)).toBeTruthy();
  });

  it("groups by reason deterministically", () => {
    const groups = byReason(plan().suppressed as never);
    expect(groups.map(([reason, items]) => [reason, items.length])).toEqual([
      ["BELOW THE BAR", 2],
      ["SAID RECENTLY", 1],
    ]);
  });

  it("notifies nobody when the page is opened", async () => {
    render(<ProactiveSection />);
    await screen.findByText("1 machine down right now");
    expect(apiPost).not.toHaveBeenCalled();
  });

  it("sends only when a person presses the button", async () => {
    vi.mocked(apiPost).mockResolvedValue({ sent: 2, held_back: 3 });
    render(<ProactiveSection />);
    const button = await screen.findByRole("button", { name: /Notify \(2\)/ });
    fireEvent.click(button);
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/proactive/send", {}));
    expect(await screen.findByText("Sent 2, held back 3.")).toBeTruthy();
  });

  it("offers no send button to a role that cannot send", async () => {
    vi.mocked(getUserRole).mockReturnValue("Operator");
    render(<ProactiveSection />);
    await screen.findByText("1 machine down right now");
    expect(screen.queryByRole("button", { name: /Notify/ })).toBeNull();
  });

  it("says it is holding things back even when it would raise nothing", async () => {
    vi.mocked(apiGet).mockResolvedValue(plan({
      headline: "Nothing worth interrupting you for. 3 things considered and held back.",
      qualified: [],
    }));
    render(<ProactiveSection />);
    expect(await screen.findByText(/Nothing worth interrupting you for/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Show the 3 AMP is holding back/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Notify/ })).toBeNull();
  });

  it("renders nothing rather than an empty shell when the load fails", async () => {
    vi.mocked(apiGet).mockRejectedValue(new Error("boom"));
    const { container } = render(<ProactiveSection />);
    await waitFor(() => expect(container.textContent).toBe(""));
  });
});
