import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CURRENCY } from "./money";

// ScorecardStrip fetches its own data, so the wire payload is the input under test.
const apiGet = vi.fn();
// ScorecardStrip imports "../lib/api"; from this file that resolves to the same
// module as "./api", so one mock covers it.
vi.mock("./api", () => ({ apiGet: (p: string) => apiGet(p) }));

import ScorecardStrip from "../components/ScorecardStrip";

// Mirrors ai/scorecard.py's loss_cost KPI: the currency symbol arrives as the
// `unit` token and the component decides prefix-vs-suffix by comparing it.
const payload = (unit: string) => ({
  has_data: true,
  kpis: [
    { key: "loss_cost", label: "Cost of losses", value: 49740, unit,
      tone: "warn", delta: -8200, delta_tone: "good" },
  ],
});

describe("scorecard money rendering", () => {
  beforeEach(() => {
    apiGet.mockReset();
  });

  it("renders the backend's currency token as a PREFIX", async () => {
    apiGet.mockResolvedValue(payload(CURRENCY));
    render(<ScorecardStrip />);
    // The real rendered DOM, not a formatter in isolation. This is the assertion a
    // screenshot would have given us, minus the login.
    await waitFor(() => expect(screen.getByText(`${CURRENCY}49,740`)).toBeTruthy());
    expect(screen.getByText(/49,740/).textContent).not.toContain("$");
    // The delta takes a separate formatting branch.
    expect(screen.getByText(new RegExp(`${CURRENCY}8,200`))).toBeTruthy();
  });

  it("is the reason the token must not drift: a stale $ unit renders as a SUFFIX", async () => {
    // THE CONTROL, and the whole point of pinning the token in two places. If the
    // backend ever ships "$" again while this component compares CURRENCY, the value
    // falls through to the generic branch and reads "49740$" — no grouping, symbol on
    // the wrong side. Asserting the broken shape proves the guard is load-bearing
    // rather than decorative.
    apiGet.mockResolvedValue(payload("$"));
    render(<ScorecardStrip />);
    await waitFor(() => expect(screen.getByText("49740$")).toBeTruthy());
    expect(screen.queryByText(`${CURRENCY}49,740`)).toBeNull();
  });
});
