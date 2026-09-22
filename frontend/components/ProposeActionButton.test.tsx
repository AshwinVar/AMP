import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The one place the frontend writes on the action path (ADR-0039).
 *
 * Two surfaces offer a draft — the Copilot's answer and the Risk Radar — and
 * they must not each carry their own copy of the request. A second
 * implementation is how one of them quietly starts sending a priority the
 * server is re-deriving anyway, or stops surfacing the refusal, and nothing
 * notices. So the payload and the refusal behaviour are pinned HERE, once.
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

import ProposeActionButton from "./ProposeActionButton";

beforeEach(() => apiPost.mockReset());
afterEach(cleanup);

describe("ProposeActionButton", () => {
  it("sends ONLY the kind and the machine id", async () => {
    apiPost.mockResolvedValue({ id: 3, summary: "Open a Critical maintenance task for CNC-01" });
    render(<ProposeActionButton kind="maintenance_task" machineId={12} />);
    fireEvent.click(screen.getByText("Propose this action"));
    await waitFor(() => expect(apiPost).toHaveBeenCalled());
    const [path, body] = apiPost.mock.calls[0];
    expect(path).toBe("/agent-actions/propose");
    expect(body).toEqual({ kind: "maintenance_task", machine_id: 12 });
    expect(Object.keys(body as object).sort()).toEqual(["kind", "machine_id"]);
  });

  it("says nothing exists yet, before anything is pressed", () => {
    render(<ProposeActionButton kind="maintenance_task" machineId={12} />);
    expect(screen.getByText(/Nothing exists yet/)).toBeTruthy();
    expect(screen.getByText(/only takes effect once somebody approves it/)).toBeTruthy();
  });

  it("after raising, says waiting for approval and NOT carried out", async () => {
    apiPost.mockResolvedValue({ id: 3, summary: "Open a Critical maintenance task for CNC-01" });
    render(<ProposeActionButton kind="maintenance_task" machineId={12} />);
    fireEvent.click(screen.getByText("Propose this action"));
    expect((await screen.findByRole("status")).textContent).toContain("waiting for approval");
    expect(screen.getByText(/Nothing has been carried out yet/)).toBeTruthy();
    // Gone, so one draft cannot be raised twice from one control.
    expect(screen.queryByText("Propose this action")).toBeNull();
  });

  it("shows the server's own refusal, and stays raisable", async () => {
    apiPost.mockRejectedValueOnce(
      new Error('{"detail":"CNC-01 already has an open maintenance task raised from the Copilot."}'),
    );
    render(<ProposeActionButton kind="maintenance_task" machineId={12} />);
    fireEvent.click(screen.getByText("Propose this action"));
    expect((await screen.findByRole("alert")).textContent).toContain("already has an open maintenance task");
    expect(screen.queryByText(/waiting for approval/)).toBeNull();
    expect(screen.getByText("Propose this action")).toBeTruthy();
  });

  it("takes its label from the caller, so each surface reads in its own voice", () => {
    render(
      <ProposeActionButton kind="maintenance_task" machineId={12} label="Propose a maintenance task" />,
    );
    expect(screen.getByText("Propose a maintenance task")).toBeTruthy();
  });

  it("compact mode drops the prose but never the promise-keeping copy", async () => {
    apiPost.mockResolvedValue({ id: 3, summary: "x" });
    render(<ProposeActionButton kind="maintenance_task" machineId={12} compact />);
    // The inline explainer is for the full card; the compact one sits under a
    // risk that already says what it is.
    expect(screen.queryByText(/Nothing exists yet/)).toBeNull();
    fireEvent.click(screen.getByText("Propose this action"));
    // ...but "not carried out yet" is never dropped, in either mode.
    expect((await screen.findByRole("status")).textContent).toContain("Nothing has been carried out yet");
  });

  it("offers a way to the approval queue once raised", async () => {
    apiPost.mockResolvedValue({ id: 3, summary: "x" });
    const onOpen = vi.fn();
    render(<ProposeActionButton kind="maintenance_task" machineId={12} onOpen={onOpen} />);
    fireEvent.click(screen.getByText("Propose this action"));
    fireEvent.click(await screen.findByText("Open Approvals →"));
    expect(onOpen).toHaveBeenCalledWith("inbox");
  });
});
