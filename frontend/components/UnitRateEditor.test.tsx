import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * A rate that did not save says so.
 *
 * The £/good-unit rate is what the recovery card and the Executive OEE money
 * panel price every loss figure from. The editor's catch was empty — "stay
 * quiet, leave things as they are" — so a refused or failed save left the
 * input open holding the typed value and the displayed rate unchanged, which
 * reads as "still typing", not "not saved".
 */

const apiPatch = vi.fn();

vi.mock("../lib/api", async () => {
  const real = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...real, apiPatch: (p: string, b: unknown) => apiPatch(p, b) };
});

import UnitRateEditor from "./UnitRateEditor";

beforeEach(() => apiPatch.mockReset());
afterEach(cleanup);

async function openAndType(value: string) {
  fireEvent.click(screen.getByRole("button", { name: "edit rate" }));
  const input = screen.getByRole("spinbutton") as HTMLInputElement;
  fireEvent.change(input, { target: { value } });
  return input;
}

describe("UnitRateEditor", () => {
  it("shows the server's reason when the save was refused, and keeps the editor open", async () => {
    // A one-shot rejection. A persistent one (mockRejectedValue, or an
    // implementation returning Promise.reject) was reported by vitest 4 as an
    // unhandled rejection against this test even though the component awaits
    // and catches it — established by elimination, not explained; the
    // component is only ever asked once here, so the one-shot form loses nothing.
    apiPatch.mockRejectedValueOnce(new Error(
      "Failed request: /tenant-config | 403 | " + JSON.stringify({ detail: "Only an Admin can set the unit value" })));
    const onSaved = vi.fn();
    render(<UnitRateEditor rate={2.5} isAdmin onSaved={onSaved} />);
    await openAndType("3.1");
    fireEvent.click(screen.getByRole("button", { name: "save" }));

    await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toBe("Not saved: Only an Admin can set the unit value");
    expect(screen.getByRole("spinbutton")).toBeTruthy();   // still editing
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("refuses a negative rate before sending it, and says so", async () => {
    render(<UnitRateEditor rate={null} isAdmin onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "set rate" }));
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "-4" } });
    fireEvent.click(screen.getByRole("button", { name: "save" }));
    expect(screen.getByRole("alert").textContent).toBe("Enter a rate of 0 or more.");
    expect(apiPatch).not.toHaveBeenCalled();
  });

  it("saves, closes and clears any earlier failure on success", async () => {
    apiPatch.mockRejectedValueOnce(new Error("Failed to fetch"));
    apiPatch.mockResolvedValueOnce({});
    const onSaved = vi.fn();
    render(<UnitRateEditor rate={2.5} isAdmin onSaved={onSaved} />);
    await openAndType("3.1");
    fireEvent.click(screen.getByRole("button", { name: "save" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("button", { name: "edit rate" })).toBeTruthy();   // closed
    expect(apiPatch).toHaveBeenLastCalledWith("/tenant-config", { unit_value_gbp: 3.1 });
  });

  it("cancel drops the failure with the draft", async () => {
    apiPatch.mockRejectedValueOnce(new Error("Failed to fetch"));
    render(<UnitRateEditor rate={2.5} isAdmin onSaved={vi.fn()} />);
    await openAndType("9");
    fireEvent.click(screen.getByRole("button", { name: "save" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "cancel" }));
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "edit rate" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
