import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The one input AMP could not be given.
 *
 * `POST /production-records` has existed since the beginning and no screen ever
 * called it — every writer was a seeder, the simulator or the MQTT ingest. So a
 * factory without a gateway (most SMEs on day one) had no way to give AMP the
 * rows OEE is computed from, and the Command Centre said "no production
 * recorded" forever. That is the whole product's headline number.
 *
 * What these tests pin is mostly refusal behaviour, because the failure mode
 * that matters is a form that LOOKS like it saved. The server validates
 * `good + rejected == total` and non-negative values; this form repeats the
 * first one so the answer is instant, and shows the server's own sentence
 * verbatim when it refuses (#695: a failure says so).
 */

const apiPost = vi.fn();
vi.mock("../lib/api", () => ({
  apiGet: vi.fn(),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  errorDetail: (e: unknown) => {
    const raw = e instanceof Error ? e.message : String(e);
    try {
      return JSON.parse(raw).detail as string;
    } catch {
      return raw;
    }
  },
}));

import ProductionEntryForm from "./ProductionEntryForm";

const MACHINES = [
  { id: 4, name: "CNC-01" },
  { id: 7, name: "LINE-01" },
];

function fill(values: Record<string, string>) {
  for (const [label, value] of Object.entries(values)) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }
}

beforeEach(() => apiPost.mockReset());
afterEach(cleanup);

describe("ProductionEntryForm", () => {
  it("says to add a machine first when there are none", () => {
    render(<ProductionEntryForm machines={[]} />);
    expect(screen.getByRole("status").textContent).toContain("Add a machine first");
    expect(screen.queryByText("Save production record")).toBeNull();
  });

  it("explains ideal cycle time, the one term that is ours and not theirs", () => {
    render(<ProductionEntryForm machines={MACHINES} />);
    expect(screen.getByText(/Seconds to make ONE good part at full speed/)).toBeTruthy();
    // And says why the row matters at all.
    expect(screen.getByText(/what AMP measures OEE from/)).toBeTruthy();
  });

  it("sends exactly what the route's schema takes", async () => {
    apiPost.mockResolvedValue({ id: 1 });
    render(<ProductionEntryForm machines={MACHINES} />);
    fill({
      Machine: "4",
      "Planned minutes": "480",
      "Runtime minutes": "456",
      "Ideal cycle time (seconds)": "30",
      Made: "900",
      Good: "891",
      Rejected: "9",
    });
    fireEvent.click(screen.getByText("Save production record"));
    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    const [path, body] = apiPost.mock.calls[0];
    expect(path).toBe("/production-records");
    expect(body).toEqual({
      machine_id: 4,
      planned_minutes: 480,
      runtime_minutes: 456,
      ideal_cycle_time_seconds: 30,
      total_count: 900,
      good_count: 891,
      rejected_count: 9,
    });
  });

  it("refuses counts that do not add up, before the round trip", () => {
    render(<ProductionEntryForm machines={MACHINES} />);
    fill({ Made: "900", Good: "800", Rejected: "50" });
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("850");
    expect(alert.textContent).toContain("900");
    expect((screen.getByText("Save production record") as HTMLButtonElement).disabled).toBe(true);
    expect(apiPost).not.toHaveBeenCalled();
  });

  it("shows the server's own refusal, not a generic failure", async () => {
    apiPost.mockRejectedValueOnce(
      new Error('{"detail":"minutes and counts must be non-negative"}'),
    );
    render(<ProductionEntryForm machines={MACHINES} />);
    fill({
      Machine: "7",
      "Runtime minutes": "456",
      "Ideal cycle time (seconds)": "30",
      Made: "900",
      Good: "891",
      Rejected: "9",
    });
    fireEvent.click(screen.getByText("Save production record"));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("must be non-negative");
    // A refusal is not a save: the form must not claim it recorded anything.
    expect(screen.queryByText(/Recorded\./)).toBeNull();
  });

  it("confirms only on a real save, and keeps the machine for the next shift", async () => {
    apiPost.mockResolvedValue({ id: 1 });
    const onSaved = vi.fn();
    render(<ProductionEntryForm machines={MACHINES} onSaved={onSaved} />);
    fill({
      Machine: "7",
      "Runtime minutes": "456",
      "Ideal cycle time (seconds)": "30",
      Made: "900",
      Good: "891",
      Rejected: "9",
    });
    fireEvent.click(screen.getByText("Save production record"));
    expect((await screen.findByRole("status")).textContent).toContain("Recorded");
    expect(onSaved).toHaveBeenCalled();
    // The counts clear for the next row; the machine does not, because entry is
    // machine-by-machine across a shift.
    expect((screen.getByLabelText("Machine") as HTMLSelectElement).value).toBe("7");
    expect((screen.getByLabelText("Made") as HTMLInputElement).value).toBe("");
  });
});
