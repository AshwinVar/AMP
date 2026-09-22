import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * The first-run experience there had never been one of.
 *
 * There was no wizard, no guided setup and not one call to action anywhere in
 * the app — 0 hits for "wizard" across the repo. The empty states were honest
 * ("No machines yet", "Fleet health not measured") and every one was a dead
 * end: the moment a new Admin most needed direction was the moment they got
 * none.
 *
 * Two rules this must not break:
 *   - it lists only steps AMP actually supports. "Create a site" is absent on
 *     purpose: `Machine.site` is in the schema and reachable from no form and
 *     no route, so a row for it would be an instruction nobody could follow;
 *   - it never claims a step is done that isn't, and it gets out of the way
 *     once the workspace is set up.
 */

import SetupChecklist, { type SetupState } from "./SetupChecklist";

const EMPTY: SetupState = {
  machines: 0,
  production: 0,
  shifts: 0,
  workOrders: 0,
  inventoryItems: 0,
  unitValueSet: false,
};
const DONE: SetupState = {
  machines: 8,
  production: 6,
  shifts: 3,
  workOrders: 10,
  inventoryItems: 7,
  unitValueSet: true,
};

afterEach(cleanup);

describe("SetupChecklist", () => {
  it("shows every step as not done on a brand-new workspace", () => {
    render(<SetupChecklist state={EMPTY} />);
    expect(screen.getByText("0 of 6 done")).toBeTruthy();
    expect(screen.getByText("Add your machines")).toBeTruthy();
    expect(screen.getByText("Record what they made")).toBeTruthy();
  });

  it("disappears once the workspace is set up", () => {
    const { container } = render(<SetupChecklist state={DONE} />);
    expect(container.firstChild).toBeNull();
  });

  it("counts only what is actually there", () => {
    render(<SetupChecklist state={{ ...EMPTY, machines: 8, shifts: 3 }} />);
    expect(screen.getByText("2 of 6 done")).toBeTruthy();
    expect(screen.getByText("8 registered.")).toBeTruthy();
    // ...and the step that depends on machines is still open.
    expect(
      screen.getByText(/This is what OEE is measured from/),
    ).toBeTruthy();
  });

  it("lists no step AMP cannot actually carry out", () => {
    render(<SetupChecklist state={EMPTY} />);
    // `Machine.site` has no form and no route; a row for it would be an
    // instruction nobody could follow.
    expect(screen.queryByText(/site/i)).toBeNull();
    expect(screen.queryByText(/connect your (plc|gateway)/i)).toBeNull();
  });

  it("says the unit value is optional, and why leaving it costs nothing", () => {
    render(<SetupChecklist state={EMPTY} />);
    expect(screen.getByText(/it will not invent a price/)).toBeTruthy();
  });

  it("offers a way to each unfinished step, and none to the finished ones", () => {
    const onOpen = vi.fn();
    render(<SetupChecklist state={{ ...EMPTY, machines: 8 }} onOpen={onOpen} />);
    expect(screen.queryByText(/Go to add your machines/i)).toBeNull();
    fireEvent.click(screen.getByText(/Go to add the materials you hold/i));
    expect(onOpen).toHaveBeenCalledWith("inventory");
  });

  it("marks done state for assistive tech, not just colour", () => {
    render(<SetupChecklist state={{ ...EMPTY, machines: 8 }} />);
    expect(screen.getAllByText("— done").length).toBe(1);
    expect(screen.getAllByText("— not done yet").length).toBe(5);
  });
});
