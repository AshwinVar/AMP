import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import DisputePanel from "./DisputePanel";
import { dispute, fakeApi, statement } from "./testFixtures";

/**
 * Disputing a window, and settling it (ADR-0020).
 *
 * A dispute names one machine and a UTC window; the typed times reach the API in
 * its whole-second form or not at all. A resolution is accepted by the OTHER
 * party only, and settles a window as something other than Disputed.
 */

function api(side: "OEM" | "FACTORY", overrides: Record<string, ReturnType<typeof vi.fn>> = {}) {
  return fakeApi(side, (name) => overrides[name] ?? vi.fn().mockResolvedValue({}));
}

function type(label: RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("DisputePanel", () => {
  it("raises a dispute with the machine, the UTC window, the bucket and the reason", async () => {
    const raiseDispute = vi.fn().mockResolvedValue({});
    const onChanged = vi.fn();
    render(<DisputePanel api={api("FACTORY", { raiseDispute })} contractId={7}
                         statement={statement()} canManage canSign onChanged={onChanged} />);
    type(/^From/, "2026-05-02T08:00");
    type(/^To/, "2026-05-02T09:30");
    type(/^Why/, "  planned changeover  ");
    fireEvent.click(screen.getByRole("button", { name: "Raise dispute" }));
    await waitFor(() => expect(raiseDispute).toHaveBeenCalledWith(7, 5, {
      installation_id: 12, window_start: "2026-05-02T08:00:00Z",
      window_end: "2026-05-02T09:30:00Z", reason: "planned changeover", proposed_bucket: "FACTORY",
    }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("refuses to send a window it cannot read as UTC", () => {
    const raiseDispute = vi.fn();
    render(<DisputePanel api={api("FACTORY", { raiseDispute })} contractId={7}
                         statement={statement()} canManage canSign onChanged={vi.fn()} />);
    type(/^From/, "02/05/2026 08:00");
    type(/^To/, "2026-05-02T09:30");
    type(/^Why/, "x");
    fireEvent.click(screen.getByRole("button", { name: "Raise dispute" }));
    expect(raiseDispute).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toMatch(/UTC date and time/);
  });

  it("offers no Disputed option for a resolution", () => {
    render(<DisputePanel api={api("OEM")} contractId={7} canManage canSign onChanged={vi.fn()}
                         statement={statement({ disputes: [dispute()] })} />);
    const options = Array.from(
      (screen.getByLabelText("Resolution for dispute 4") as HTMLSelectElement).options,
    ).map((o) => o.value);
    expect(options).toEqual(["AVAILABLE", "OEM", "FACTORY", "UNMEASURED"]);
  });

  it("lets only the OTHER party accept a proposed resolution", async () => {
    const proposed = dispute({ status: "resolution_proposed", resolution_bucket: "FACTORY",
                               resolution_proposed_by_party: "FACTORY" });
    const acceptResolution = vi.fn().mockResolvedValue({});
    const { unmount } = render(
      <DisputePanel api={api("FACTORY", { acceptResolution })} contractId={7} canManage canSign
                    onChanged={vi.fn()} statement={statement({ disputes: [proposed] })} />);
    expect(screen.queryByRole("button", { name: /Accept resolution/ })).toBeNull();
    unmount();

    render(<DisputePanel api={api("OEM", { acceptResolution })} contractId={7} canManage canSign
                         onChanged={vi.fn()} statement={statement({ disputes: [proposed] })} />);
    fireEvent.click(screen.getByRole("button", { name: "Accept resolution: Factory" }));
    await waitFor(() => expect(acceptResolution).toHaveBeenCalledWith(7, 4, "FACTORY"));
  });

  it("lets only the raising party withdraw, and nobody raise on an agreed statement", () => {
    const { unmount } = render(
      <DisputePanel api={api("OEM")} contractId={7} canManage canSign onChanged={vi.fn()}
                    statement={statement({ disputes: [dispute()] })} />);
    expect(screen.queryByRole("button", { name: "Withdraw" })).toBeNull();
    unmount();
    render(<DisputePanel api={api("FACTORY")} contractId={7} canManage canSign onChanged={vi.fn()}
                         statement={statement({ agreed: true, disputes: [dispute({ status: "resolved",
                           resolution_bucket: "FACTORY" })] })} />);
    expect(screen.queryByRole("button", { name: "Raise dispute" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Withdraw" })).toBeNull();
    expect(screen.getByTestId("dispute-4").textContent).toMatch(/Resolution agreed.*Factory/);
  });
});
