import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Transport Tasks' obligations:
 *  - the form offers only REAL stations (from /fleet/state) and posts the
 *    four fields the dispatcher needs;
 *  - a server refusal surfaces as the server's own sentence, not a shrug;
 *  - Cancel appears only on rows the server would actually cancel
 *    (Pending/Assigned) and only for a role it obeys;
 *  - the settled list shows Delivered/Cancelled history, not live rows.
 */

const apiGet = vi.fn();
const apiPost = vi.fn();
let role = "Admin";

vi.mock("../lib/api", () => ({
  apiGet: (p: string) => apiGet(p),
  apiPost: (p: string, b: unknown) => apiPost(p, b),
  errorDetail: (e: unknown) => (e instanceof Error ? e.message : String(e)),
  getUserRole: () => role,
}));

import TransportTasksSection from "./TransportTasksSection";

const STATE = {
  stations: {
    Warehouse: { x: 8, y: 15, kind: "anchor" },
    Packing: { x: 92, y: 30, kind: "anchor" },
    "Charge Dock": { x: 8, y: 85, kind: "anchor" },
    "IMM-01": { x: 50, y: 50, kind: "machine" },
  },
  vehicles: [{ id: 1, name: "AMR-01" }],
  tasks: [
    { id: 11, origin: "IMM-01", destination: "Packing", payload: "Finished batch",
      priority: 3, status: "Pending", vehicle_id: null, source: "mes",
      requested_at: "2026-11-10T09:00:00", assigned_at: null,
      picked_up_at: null, completed_at: null },
    { id: 12, origin: "Warehouse", destination: "IMM-01", payload: "",
      priority: 2, status: "InTransit", vehicle_id: 1, source: "manual",
      requested_at: "2026-11-10T09:01:00", assigned_at: "2026-11-10T09:01:10",
      picked_up_at: "2026-11-10T09:02:00", completed_at: null },
  ],
  kpis: { pending_tasks: 1, active_tasks: 1, delivered_24h: 4, avg_wait_s: 9.5 },
};

const HISTORY = [
  ...STATE.tasks,
  { id: 7, origin: "IMM-01", destination: "Packing", payload: "Bin 2",
    priority: 2, status: "Delivered", vehicle_id: 1, source: "manual",
    requested_at: "2026-11-10T08:00:00", assigned_at: "2026-11-10T08:00:05",
    picked_up_at: "2026-11-10T08:01:00", completed_at: "2026-11-10T08:03:00" },
];

afterEach(cleanup);

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  role = "Admin";
  apiGet.mockImplementation((p: string) => {
    if (p === "/fleet/state") return Promise.resolve(STATE);
    if (p.startsWith("/fleet/tasks")) return Promise.resolve(HISTORY);
    return Promise.resolve(null);
  });
  apiPost.mockResolvedValue({});
});

describe("TransportTasksSection", () => {
  it("renders the live queue and offers only real stations (no Charge Dock)", async () => {
    render(<TransportTasksSection />);
    expect(await screen.findByText("Finished batch")).toBeTruthy();
    const originSelect = screen.getAllByRole("combobox")[0] as HTMLSelectElement;
    const options = Array.from(originSelect.options).map((o) => o.value);
    expect(options).toContain("IMM-01");
    expect(options).toContain("Warehouse");
    expect(options).not.toContain("Charge Dock");
  });

  it("dispatching posts the four fields the engine needs", async () => {
    render(<TransportTasksSection />);
    await screen.findByText("Finished batch");
    const [originSel, destSel] = screen.getAllByRole("combobox");
    fireEvent.change(originSel, { target: { value: "IMM-01" } });
    fireEvent.change(destSel, { target: { value: "Packing" } });
    fireEvent.change(screen.getByPlaceholderText(/finished parts/), {
      target: { value: "Bin 5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Dispatch it" }));
    await screen.findByText("Transport Tasks");
    expect(apiPost).toHaveBeenCalledWith("/fleet/tasks", {
      origin: "IMM-01", destination: "Packing", payload: "Bin 5", priority: 2,
    });
  });

  it("a server refusal shows the server's own sentence", async () => {
    apiPost.mockRejectedValueOnce(new Error("origin and destination must differ"));
    render(<TransportTasksSection />);
    await screen.findByText("Finished batch");
    const [originSel, destSel] = screen.getAllByRole("combobox");
    fireEvent.change(originSel, { target: { value: "Packing" } });
    fireEvent.change(destSel, { target: { value: "Packing" } });
    fireEvent.click(screen.getByRole("button", { name: "Dispatch it" }));
    expect(await screen.findByText("origin and destination must differ")).toBeTruthy();
  });

  it("Cancel appears on Pending rows only, and posts the cancel", async () => {
    render(<TransportTasksSection />);
    await screen.findByText("Finished batch");
    const cancels = screen.getAllByRole("button", { name: "Cancel" });
    expect(cancels.length).toBe(1);          // Pending yes, InTransit no
    fireEvent.click(cancels[0]);
    await screen.findByText("Transport Tasks");
    expect(apiPost).toHaveBeenCalledWith("/fleet/tasks/11/cancel", {});
  });

  it("an Operator can raise orders but not cancel them", async () => {
    role = "Operator";
    render(<TransportTasksSection />);
    await screen.findByText("Finished batch");
    expect(screen.getByRole("button", { name: "Dispatch it" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("the settled list shows history, not live rows", async () => {
    render(<TransportTasksSection />);
    await screen.findByText("Recently settled");
    expect(await screen.findByText("Bin 2")).toBeTruthy();   // the Delivered row
    // The Pending row (#11) renders in the live queue, not in settled —
    // its payload appears exactly once across the page.
    expect(screen.getAllByText("Finished batch").length).toBe(1);
  });
});
