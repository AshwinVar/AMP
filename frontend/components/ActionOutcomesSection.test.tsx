import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

import ActionOutcomesSection, { reading } from "./ActionOutcomesSection";

/**
 * "Did it help?" is the easiest card in the product to overclaim on (ADR-0029).
 *
 * What is pinned is not the layout: it is that a verdict word never floats free
 * of the metric it describes, that nothing inside its window gets a verdict at
 * all, that "no reading" is never rendered as a zero, and that the caveat is on
 * the card rather than behind a toggle.
 */
vi.mock("../lib/api", () => ({ apiGet: vi.fn() }));
const { apiGet } = await import("../lib/api");

const NOTE =
  "AMP measured what changed after the decision. It cannot show the action caused the change: nothing else in the plant was held still.";

const outcome = (over: Record<string, unknown> = {}) => ({
  id: 1,
  action_id: 11,
  action: "Service CNC-01",
  agent: "maintenance",
  metric_label: "Downtime",
  unit: "min",
  rule: "minutes of logged stoppage on this machine",
  better: "lower",
  scope_label: "CNC-01",
  window_days: 7,
  baseline_value: 90,
  measured_value: 20,
  change: -70,
  waiting: false,
  days_left: 0,
  verdict: "BETTER",
  ...over,
});

const summary = (over: Record<string, unknown> = {}) => ({
  state: "OK",
  headline: "Of 1 followed up, 1 got better, 0 got worse, 0 did not move, and 0 could not be measured.",
  window_days: 7,
  followed_up: 1,
  waiting: 0,
  measured: 1,
  counts: { BETTER: 1, WORSE: 0, "NO CHANGE": 0, "NOT MEASURABLE": 0 },
  outcomes: [outcome()],
  note: NOTE,
  ...over,
});

describe("ActionOutcomesSection", () => {
  beforeEach(() => {
    vi.mocked(apiGet).mockReset();
    vi.mocked(apiGet).mockResolvedValue(summary());
  });

  it("puts the caveat on the card, not behind a toggle", async () => {
    render(<ActionOutcomesSection />);
    expect(await screen.findByText(/cannot show the action caused the change/)).toBeTruthy();
  });

  it("never shows a verdict without the metric it describes", async () => {
    const { container } = render(<ActionOutcomesSection />);
    await screen.findByText("BETTER");
    const item = container.querySelector("li");
    expect(item?.textContent).toContain("BETTER");
    expect(item?.textContent).toContain("Downtime");
    expect(item?.textContent).toContain("minutes of logged stoppage on this machine");
  });

  it("shows both readings and which way is good", async () => {
    render(<ActionOutcomesSection />);
    expect(await screen.findByText(/before 90 min → after 20 min/)).toBeTruthy();
    expect(screen.getByText(/lower is better/)).toBeTruthy();
  });

  it("gives no verdict at all inside the window, only the time left", async () => {
    vi.mocked(apiGet).mockResolvedValue(summary({
      state: "INSUFFICIENT HISTORY",
      headline: "1 approved action is still inside the window AMP measures.",
      measured: 0,
      waiting: 1,
      outcomes: [outcome({ waiting: true, days_left: 3, verdict: null, measured_value: null, change: null })],
    }));
    const { container } = render(<ActionOutcomesSection />);
    expect(await screen.findByText("3d to go")).toBeTruthy();
    expect(container.textContent).not.toContain("BETTER");
    expect(container.textContent).not.toContain("WORSE");
    expect(container.textContent).toContain("measured after the window");
  });

  it("renders a missing reading as 'no reading', never as 0", async () => {
    vi.mocked(apiGet).mockResolvedValue(summary({
      outcomes: [outcome({ baseline_value: null, measured_value: 12, change: null,
                           verdict: "NOT MEASURABLE" })],
    }));
    render(<ActionOutcomesSection />);
    expect(await screen.findByText(/before no reading → after 12 min/)).toBeTruthy();
    expect(screen.getByText("NOT MEASURABLE")).toBeTruthy();
  });

  it("formats readings without inventing precision", () => {
    expect(reading(null, "min")).toBe("no reading");
    expect(reading(0, "min")).toBe("0 min");
    expect(reading(1234, "units")).toBe("1,234 units");
    expect(reading(12.34, "min")).toBe("12.3 min");
  });

  it("says plainly when there is nothing to show yet", async () => {
    vi.mocked(apiGet).mockResolvedValue(summary({
      state: "NO DATA", headline: "No approved action has been followed up yet.",
      followed_up: 0, measured: 0, outcomes: [],
    }));
    render(<ActionOutcomesSection />);
    expect(await screen.findByText(/AMP starts measuring the moment an action is approved/)).toBeTruthy();
  });

  it("renders nothing rather than an empty shell when the load fails", async () => {
    vi.mocked(apiGet).mockRejectedValue(new Error("boom"));
    const { container } = render(<ActionOutcomesSection />);
    await waitFor(() => expect(container.textContent).toBe(""));
  });
});
