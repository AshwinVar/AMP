import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import DailyBriefSection, { briefText } from "./DailyBriefSection";

/**
 * The brief card (ADR-0028).
 *
 * What is pinned is what makes it a brief rather than a summary: the window is
 * always on screen, the "what AMP could not see" section is never behind a
 * toggle, and the copied text is the same words in the same order — an owner
 * who pastes it into an email must not paste a rosier version than the screen.
 */
vi.mock("../lib/api", () => ({ apiGet: vi.fn() }));
const { apiGet } = await import("../lib/api");

const brief = (over: Record<string, unknown> = {}) => ({
  generated_at: "2026-09-20T08:00:00",
  window: "the last 7 days, ending 2026-09-20",
  state: "PARTIAL DATA",
  headline: "CNC-01 stopped leads the day. 2 things AMP could not see — see the last section.",
  sections: [
    { key: "position", title: "Where we are", lines: ["Plant OEE is 53% from 2 of 3 machines."], state: "PARTIAL DATA", view: "overview" },
    { key: "actions", title: "What to do next", lines: ["Nothing is waiting for a decision."], state: "OK", view: "agents" },
    { key: "shifts", title: "How the shifts did", lines: ["Day shift made 1,040 of 1,200."], state: "OK", view: "shifts" },
  ],
  blind_spots: [
    { key: "coverage", state: "PARTIAL DATA", text: "2 of 3 machines reported production. The rest are not zero, they are unmeasured." },
    { key: "no_unit_value", state: "NOT CONFIGURED", text: "No unit value is set, so nothing here is costed in money." },
  ],
  note: "Every figure here comes from a read-model you can open.",
  ...over,
});

describe("DailyBriefSection", () => {
  beforeEach(() => {
    vi.mocked(apiGet).mockReset();
    vi.mocked(apiGet).mockResolvedValue(brief());
  });

  it("always shows the window it covers", async () => {
    render(<DailyBriefSection />);
    expect(await screen.findByText("the last 7 days, ending 2026-09-20")).toBeTruthy();
  });

  it("shows what AMP could not see without needing a click", async () => {
    render(<DailyBriefSection />);
    expect(await screen.findByText(/2 of 3 machines reported production/)).toBeTruthy();
    expect(screen.getByText(/No unit value is set/)).toBeTruthy();
    expect(screen.getByText(/What AMP could not see \(2\)/)).toBeTruthy();
  });

  // A BRIEF OPENS ON ITS ANSWER. Every section used to render collapsed, and
  // `open` was a single key, so opening one closed another. Measured on the demo
  // plant: 32 lines across seven sections, and an owner saw none of them — seven
  // closed headers and the amber caveat box. That box is right to be always
  // visible; the defect was that it was the ONLY thing visible, so the one thing
  // a morning brief always showed was its own disclaimer.
  it("opens on where we are, what is wrong and what to do", async () => {
    render(<DailyBriefSection />);
    expect(await screen.findByText(/Plant OEE is 53%/)).toBeTruthy();
    expect(screen.getByText(/Nothing is waiting for a decision/)).toBeTruthy();
  });

  it("leaves the supporting sections one tap away", async () => {
    render(<DailyBriefSection />);
    await screen.findByRole("button", { name: /How the shifts did/ });
    expect(screen.queryByText(/Day shift made 1,040/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /How the shifts did/ }));
    expect(screen.getByText(/Day shift made 1,040/)).toBeTruthy();
  });

  it("opening one section does not close another", async () => {
    // `open` was a single key, so reading the shifts cost you the problems.
    render(<DailyBriefSection />);
    await screen.findByText(/Plant OEE is 53%/);
    fireEvent.click(screen.getByRole("button", { name: /How the shifts did/ }));
    expect(screen.getByText(/Day shift made 1,040/)).toBeTruthy();
    expect(screen.getByText(/Plant OEE is 53%/)).toBeTruthy();
  });

  it("an opened section can be closed again", async () => {
    render(<DailyBriefSection />);
    const toggle = await screen.findByRole("button", { name: /Where we are/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText(/Plant OEE is 53%/)).toBeNull();
  });

  it("carries the brief's own data state as a notice", async () => {
    render(<DailyBriefSection />);
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("PARTIAL DATA"));
    expect(screen.getByRole("status").textContent).toContain("part of the plant did not report");
  });

  it("copies the same words that are on the screen, blind spots included", () => {
    const text = briefText(brief() as never);
    expect(text).toContain("the last 7 days, ending 2026-09-20");
    expect(text).toContain("WHAT AMP COULD NOT SEE");
    expect(text).toContain("2 of 3 machines reported production");
    expect(text).toContain("No unit value is set");
    // The headline's own warning must survive the copy.
    expect(text).toContain("2 things AMP could not see");
    // Order: the sections, then the gaps, then the note.
    expect(text.indexOf("WHERE WE ARE")).toBeLessThan(text.indexOf("WHAT AMP COULD NOT SEE"));
    expect(text.indexOf("WHAT AMP COULD NOT SEE")).toBeLessThan(text.indexOf("read-model you can open"));
  });

  it("says what it cannot see even when nothing is missing", async () => {
    vi.mocked(apiGet).mockResolvedValue(brief({
      state: "OK",
      headline: "Nothing is ranked as a problem and nothing is likely to become one.",
      blind_spots: [{ key: "none", state: "OK", text: "Every machine reported. AMP still only sees what is recorded in it." }],
    }));
    render(<DailyBriefSection />);
    expect(await screen.findByText(/AMP still only sees what is recorded in it/)).toBeTruthy();
  });

  it("renders nothing rather than an empty shell when the load fails", async () => {
    vi.mocked(apiGet).mockRejectedValue(new Error("boom"));
    const { container } = render(<DailyBriefSection />);
    await waitFor(() => expect(container.textContent).toBe(""));
  });
});
