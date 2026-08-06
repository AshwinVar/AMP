import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { LoadError, useLoadError } from "./useLoadError";

/**
 * Pins the behaviour fixed in #383: a failed list fetch used to render as
 * "you have no data", because `.catch(() => {})` left `rows` at `[]` and the
 * component showed its empty state. A dropped connection, an expired token and
 * a 500 all looked like an empty inventory.
 *
 * These were verified by hand in a browser at the time. Encoding them here is
 * the point of adding a test runner.
 */
describe("useLoadError", () => {
  it("records a failure so the caller can render it instead of an empty state", async () => {
    const { result } = renderHook(() => useLoadError());
    const apply = vi.fn();

    await act(async () => {
      await result.current.track(Promise.reject(new Error("boom")), apply, "remnants");
    });

    expect(apply).not.toHaveBeenCalled();
    expect(result.current.error).toContain("remnants");
  });

  it("clears the error on the next success", async () => {
    const { result } = renderHook(() => useLoadError());
    const apply = vi.fn();

    await act(async () => {
      await result.current.track(Promise.reject(new Error("boom")), apply, "remnants");
    });
    expect(result.current.error).not.toBeNull();

    // A stale error left over a successful reload would be its own bug: the user
    // would see "could not load" above rows that had just loaded fine.
    await act(async () => {
      await result.current.track(Promise.resolve([1, 2]), apply, "remnants");
    });

    expect(result.current.error).toBeNull();
    expect(apply).toHaveBeenCalledWith([1, 2]);
  });

  it("names the thing that failed, so one page can distinguish its loaders", async () => {
    const { result } = renderHook(() => useLoadError());

    await act(async () => {
      await result.current.track(Promise.reject(new Error("x")), vi.fn(), "cycle-count history");
    });

    expect(result.current.error).toContain("cycle-count history");
  });

  it("applies the value and stays clean when the request succeeds first time", async () => {
    const { result } = renderHook(() => useLoadError());
    const apply = vi.fn();

    await act(async () => {
      await result.current.track(Promise.resolve(["a"]), apply, "items");
    });

    expect(result.current.error).toBeNull();
    expect(apply).toHaveBeenCalledWith(["a"]);
  });
});

/**
 * `LoadError` is the half of #383 the user actually sees, and until now nothing
 * exercised it. The hook can record a failure perfectly and the fix still ships
 * broken if the component decides not to render it — the reassuring empty state
 * is then the only thing on the page again, which is the exact bug #383 closed.
 */
describe("LoadError", () => {
  it("announces the failure rather than leaving it as text to scroll past", () => {
    render(<LoadError message="Could not load remnants. Check your connection and try again." />);

    // role="alert" is the load-bearing part: it is what makes a stock controller
    // who is already looking at the table hear about a failure that happened
    // above the fold, instead of reading a stale row count as fact.
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toBe("Could not load remnants. Check your connection and try again.");
  });

  it("renders nothing at all when there is no error", () => {
    // Control for the case above: that test proves the alert DOES render for a
    // real message, so "no alert" here means suppressed, not "this component
    // never renders anything".
    //
    // And it has to be nothing, not an empty alert. Every caller drops
    // <LoadError message={error} /> straight into a stacked column
    // (GmatsInventory, EnterpriseInventory, IndustrialConnectivity), so an
    // always-rendered <p> would open a gap under the heading of every healthy
    // page and park a permanently-live alert region in the DOM.
    const { container } = render(<LoadError message={null} />);

    expect(container.firstChild).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("treats an empty message as no error, not as an error with nothing to say", () => {
    // `setError` is exported, so a caller can push back "" from a response body
    // that had no detail in it. A `message === null` guard would let that
    // through and paint an empty red bar the user can neither read nor act on.
    const { container } = render(<LoadError message="" />);

    expect(container.firstChild).toBeNull();
  });
});

/**
 * A list that legitimately renders empty is where #383 actually bit, so drive
 * the hook and the component together the way a real panel wires them.
 */
function RemnantPanel({ load }: { load: () => Promise<number[]> }) {
  const [rows, setRows] = useState<number[]>([]);
  const { error, track } = useLoadError();

  return (
    <div>
      <button onClick={() => void track(load(), setRows, "remnants")}>Reload</button>
      <LoadError message={error} />
      {/* The gating the hook's doc comment asks callers for. */}
      {!error && rows.length === 0 && <p>No remnants logged yet.</p>}
      <ul>
        {rows.map((r) => (
          <li key={r}>Remnant {r}</li>
        ))}
      </ul>
    </div>
  );
}

describe("useLoadError driving LoadError", () => {
  it("puts the failure in place of the empty state, and takes it back down on a good reload", async () => {
    const load = vi
      .fn()
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValueOnce([7]);

    render(<RemnantPanel load={load} />);
    // The empty state is genuinely reachable — otherwise the assertion below
    // that it disappeared would pass against a panel that never showed it.
    expect(screen.getByText("No remnants logged yet.")).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    });

    // "No remnants logged yet." after a dropped request is the sentence a stock
    // controller may act on. It must be gone, and the reason must be on screen.
    expect(screen.getByRole("alert").textContent).toContain("remnants");
    expect(screen.queryByText("No remnants logged yet.")).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    });

    // A banner left standing over rows that just loaded fine is how operators
    // learn to ignore the banner, so the retry has to clear it.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("Remnant 7")).toBeTruthy();
  });
});
